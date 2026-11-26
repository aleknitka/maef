"""Validate the definition files in a repository checkout (or a draft worktree).

Interface: `lint_repo(root, only_agent=None) -> list[Finding]`. Pure reads: no network, no git.
Checked: every `agents/*/agent.yaml` (schema, id matches directory, instruction/skill files exist inside
the agent directory, built-in tools, MCP references resolve to exact versions, network_open consistency,
sandbox profile), plus `sandbox-profiles/`, `mcp-servers/` and `model-profiles/` when present.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from .definitions import mcp_ref_problem, parse_mcp_ref
from .gitrepo import safe_relpath
from .models import (
    AgentDefinition,
    McpServerManifest,
    ModelEndpointProfile,
    SandboxProfile,
)
from .tools import BUILTIN_TOOL_NAMES, SANDBOXED_TOOLS


class Finding(BaseModel):
    path: str  # repository-relative, `/` separated
    message: str
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.severity == 'warning' else ''}{self.message}"


def _load(path: Path, model: type[BaseModel], rel: str, out: list[Finding]):
    """Parse one YAML file into `model`, appending a finding per problem; None when unusable."""
    try:
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(value)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            out.append(Finding(path=rel, message=f"{loc}: {err['msg']}" if loc else err["msg"]))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        out.append(Finding(path=rel, message=f"unreadable or invalid: {type(exc).__name__}"))
    return None


def _dir_files(root: Path, name: str) -> list[Path]:
    directory = root / name
    return sorted(directory.glob("*.yaml")) if directory.is_dir() else []


def _lint_files(root: Path, directory: str, model: type[BaseModel], out: list[Finding]) -> dict:
    loaded = {}
    for path in _dir_files(root, directory):
        rel = f"{directory}/{path.name}"
        item = _load(path, model, rel, out)
        if item is None:
            continue
        if item.id != path.stem:
            out.append(Finding(path=rel, message=f"id {item.id!r} does not match file name {path.stem!r}"))
        loaded[path.stem] = item
    return loaded


def _lint_agent(
    root: Path,
    agent_dir: Path,
    mcps: dict[str, McpServerManifest],
    sandboxes: dict,
    have_model_profiles: bool,
    models: dict,
    out: list[Finding],
) -> None:
    rel_dir = f"agents/{agent_dir.name}"
    definition = agent_dir / "agent.yaml"
    if not definition.is_file():
        out.append(Finding(path=f"{rel_dir}/agent.yaml", message="missing"))
        return
    rel = f"{rel_dir}/agent.yaml"
    agent = _load(definition, AgentDefinition, rel, out)
    if agent is None:
        return
    if agent.id != agent_dir.name:
        out.append(Finding(path=rel, message=f"id {agent.id!r} does not match directory {agent_dir.name!r}"))
    base = agent_dir.resolve()
    for name in (agent.instructions, *agent.skills):
        try:
            safe_relpath(name)
            path = base / name
            ok = not path.is_symlink() and path.resolve().is_relative_to(base) and path.is_file()
        except (ValueError, OSError):
            ok = False
        if not ok:
            out.append(Finding(path=rel, message=f"file missing or outside the agent directory: {name}"))
        elif name == agent.instructions:
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                out.append(Finding(path=f"{rel_dir}/{name}", message="instructions are not valid UTF-8"))
    if len(set(agent.skills)) != len(agent.skills):
        out.append(Finding(path=rel, message="skills lists a file twice"))
    for tool in agent.tools:
        if tool not in BUILTIN_TOOL_NAMES:
            out.append(Finding(path=rel, message=f"unknown tool: {tool}"))
    manifests: list[McpServerManifest] = []
    seen: set[str] = set()
    for ref in agent.mcp_servers:
        try:
            server_id, version = parse_mcp_ref(ref)
        except ValueError as exc:
            out.append(Finding(path=rel, message=str(exc)))
            continue
        if server_id in seen:
            out.append(Finding(path=rel, message=f"MCP server listed twice: {server_id}"))
            continue
        seen.add(server_id)
        manifest = mcps.get(server_id)
        if manifest is None:
            out.append(Finding(path=rel, message=f"MCP server not found: {server_id}"))
        elif manifest.version != version:
            out.append(
                Finding(
                    path=rel, message=f"MCP server {server_id} is version {manifest.version}, agent requires {version}"
                )
            )
        else:
            manifests.append(manifest)
    if len(manifests) == len(agent.mcp_servers) and mcp_ref_problem(agent, manifests) == "network_not_permitted":
        out.append(
            Finding(
                path=rel,
                message=(
                    "an MCP server needs open network (network: open or allowlist) "
                    "but permissions.network_open is not set"
                ),
            )
        )
    elif agent.permissions.network_open and not any(m.network != "none" for m in manifests):
        out.append(
            Finding(
                path=rel,
                severity="warning",
                message="permissions.network_open is set but no referenced MCP server needs open network",
            )
        )
    sandbox_id = agent.runtime.sandbox_profile
    if sandbox_id not in sandboxes:
        needed = any(t in SANDBOXED_TOOLS for t in agent.tools)
        out.append(
            Finding(
                path=rel, severity="error" if needed else "warning", message=f"sandbox profile not found: {sandbox_id}"
            )
        )
    if have_model_profiles and agent.runtime.model_profile not in models:
        out.append(
            Finding(
                path=rel,
                severity="warning",
                message=f"model profile not found in model-profiles/: {agent.runtime.model_profile}",
            )
        )


def lint_repo(root: Path | str, only_agent: str | None = None) -> list[Finding]:
    """All findings under `root`, ordered by path. `only_agent` limits the check to one agent directory
    (the manifests and profiles it references are still read, but their own files are not linted)."""
    root = Path(root)
    out: list[Finding] = []
    if only_agent is None:
        sandboxes = _lint_files(root, "sandbox-profiles", SandboxProfile, out)
        mcps = _lint_files(root, "mcp-servers", McpServerManifest, out)
        models = _lint_files(root, "model-profiles", ModelEndpointProfile, out)
        for server_id, m in mcps.items():
            if m.network == "allowlist" and any(h.strip() == "*" for h in m.allowed_hosts):
                out.append(
                    Finding(
                        path=f"mcp-servers/{server_id}.yaml",
                        severity="warning",
                        message=(
                            'allowed_hosts contains "*": the server can reach any public host, which is also a way '
                            "to send data out; list the hosts it needs"
                        ),
                    )
                )
    else:
        quiet: list[Finding] = []
        sandboxes = _lint_files(root, "sandbox-profiles", SandboxProfile, quiet)
        mcps = _lint_files(root, "mcp-servers", McpServerManifest, quiet)
        models = _lint_files(root, "model-profiles", ModelEndpointProfile, quiet)
    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            if only_agent is None or agent_dir.name == only_agent:
                _lint_agent(root, agent_dir, mcps, sandboxes, (root / "model-profiles").is_dir(), models, out)
    if only_agent is not None and not (agents_dir / only_agent).is_dir():
        out.append(Finding(path=f"agents/{only_agent}", message="agent directory missing"))
    return sorted(out, key=lambda f: (f.path, f.severity != "error", f.message))
