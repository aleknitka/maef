"""Pin an agent definition to exact content so a run can be reproduced and audited.

Every file is read once; the same bytes are digested and used, so what ran is what was pinned.
"""
import hashlib
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import (
    AgentDefinition,
    McpServerManifest,
    ModelEndpointProfile,
    SandboxProfile,
)
from .registry import (
    AgentRegistry,
    McpServerRegistry,
    ModelProfileRegistry,
    SandboxProfileRegistry,
)
from .tools import BUILTIN_TOOL_NAMES, SANDBOXED_TOOLS


class ResolveError(Exception):
    """The definition cannot be pinned (unknown agent, profile or unreadable file)."""


class PinnedDefinition(BaseModel):
    """An agent and its model profile pinned to exact content, ready for admission."""
    # Bytes cross the worker boundary as base64 JSON.
    model_config = ConfigDict(frozen=True, ser_json_bytes="base64", val_json_bytes="base64")
    agent: AgentDefinition
    profile: ModelEndpointProfile
    # The system prompt, decoded from the exact bytes included in definition_digest.
    instructions: str
    # Instructions and skills by agent-relative path, as pinned; tools read these, never the live tree.
    files: dict[str, bytes] = Field(default_factory=dict)
    # The exact agent.yaml bytes (the one digested file not in `files`), so a worker can re-verify the digest.
    definition_bytes: bytes = b""
    git_rev: str | None
    # True when the agent directory has uncommitted changes; the digest still pins the content.
    dirty: bool
    definition_digest: str
    profile_digest: str
    # The sandbox profile pinned the same way; None when the agent uses no sandboxed tools and none was found.
    sandbox: SandboxProfile | None = None
    sandbox_digest: str | None = None
    # Pinned MCP server manifests, in the agent's order, and their exact bytes by server id
    # (digested, so a worker can re-verify them).
    mcp_servers: list[McpServerManifest] = Field(default_factory=list)
    mcp_bytes: dict[str, bytes] = Field(default_factory=dict)


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _digest(parts: list[tuple[str, bytes]]) -> str:
    """sha256 over (name, length, bytes) records, so file boundaries cannot be shifted."""
    h = hashlib.sha256()
    for name, data in parts:
        h.update(f"{name}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


def compute_definition_digest(agent_yaml: bytes, files: Mapping[str, bytes],
                              mcp_bytes: Mapping[str, bytes] | None = None) -> str:
    """The digest recorded at admission: agent.yaml first, then `files` in pinned order, then each
    MCP manifest (as `mcp-servers/<id>.yaml`, sorted by id; none adds nothing, so definitions
    without MCP servers keep their earlier digests). Workers recompute it over the bytes they were handed."""
    mcp = [(f"mcp-servers/{sid}.yaml", data) for sid, data in sorted((mcp_bytes or {}).items())]
    return _digest([("agent.yaml", agent_yaml), *files.items(), *mcp])


def parse_mcp_ref(ref: str) -> tuple[str, str]:
    """"<id>@<version>" -> (id, version); ValueError otherwise."""
    server_id, sep, version = ref.partition("@")
    if not sep or not server_id or not version or "@" in version:
        raise ValueError(f"MCP server reference must be <id>@<version>: {ref}")
    return server_id, version


def mcp_ref_problem(agent: AgentDefinition, manifests: list[McpServerManifest]) -> str | None:
    """Why `manifests` do not satisfy the agent's references and permissions, or None."""
    try:
        refs = [parse_mcp_ref(r) for r in agent.mcp_servers]
    except ValueError:
        return "invalid_mcp_reference"
    if refs != [(m.id, m.version) for m in manifests]:
        return "mcp_reference_mismatch"
    if any(m.network == "open" for m in manifests) and not agent.permissions.network_open:
        return "network_not_permitted"
    return None


class Definitions:
    def __init__(self, agents: AgentRegistry, profiles: ModelProfileRegistry,
                 sandboxes: SandboxProfileRegistry | None = None, mcps: McpServerRegistry | None = None):
        self.agents = agents
        self.profiles = profiles
        self.sandboxes = sandboxes
        self.mcps = mcps

    def _pin_mcp(self, agent: AgentDefinition) -> tuple[list[McpServerManifest], dict[str, bytes]]:
        manifests: list[McpServerManifest] = []
        raw: dict[str, bytes] = {}
        for ref in agent.mcp_servers:
            try:
                server_id, version = parse_mcp_ref(ref)
            except ValueError as exc:
                raise ResolveError(str(exc)) from None
            if server_id in raw:
                raise ResolveError(f"MCP server listed twice: {server_id}")
            try:
                loaded = self.mcps.load(server_id) if self.mcps is not None else None
            except (OSError, ValueError, yaml.YAMLError):  # unreadable, bad YAML or invalid manifest (pydantic errors are ValueErrors)
                raise ResolveError(f"MCP server manifest unreadable or invalid: {server_id}") from None
            if loaded is None:
                raise ResolveError(f"MCP server not found: {server_id}")
            manifest, data = loaded
            if manifest.version != version:
                raise ResolveError(f"MCP server {server_id} is version {manifest.version}, agent requires {version}")
            manifests.append(manifest)
            raw[server_id] = data
        problem = mcp_ref_problem(agent, manifests)
        if problem == "network_not_permitted":
            raise ResolveError("MCP server needs open network but the agent does not set permissions.network_open")
        return manifests, raw

    def _pin_sandbox(self, profile_id: str, required: bool) -> tuple[SandboxProfile | None, str | None]:
        try:
            if self.sandboxes is None or self.sandboxes.get_profile(profile_id) is None:
                raise ResolveError(f"Sandbox profile not found: {profile_id}")
            data = (self.sandboxes.root / f"{profile_id}.yaml").read_bytes()
            return SandboxProfile.model_validate(yaml.safe_load(data)), hashlib.sha256(data).hexdigest()
        except (ResolveError, OSError, yaml.YAMLError, ValidationError) as exc:
            if required:
                if isinstance(exc, ResolveError):
                    raise
                raise ResolveError(f"Sandbox profile unreadable or invalid: {profile_id}") from None
            return None, None

    def resolve(self, agent_id: str) -> PinnedDefinition:
        agent = self.agents.get_agent(agent_id)
        if agent is None:
            raise ResolveError(f"Unknown agent: {agent_id}")
        base = (self.agents.root / agent_id).resolve()
        for tool in agent.tools:
            if tool not in BUILTIN_TOOL_NAMES:
                raise ResolveError(f"Unknown tool: {tool}")

        # Only files inside the agent directory may be pinned; symlinks could escape it.
        names = ["agent.yaml", agent.instructions, *sorted(agent.skills)]
        parts: list[tuple[str, bytes]] = []
        for name in names:
            path = base / name
            if path.is_symlink() or not path.resolve().is_relative_to(base) or not path.is_file():
                raise ResolveError(f"Agent file missing or outside the agent directory: {name}")
            parts.append((name, path.read_bytes()))
        try:
            instructions = dict(parts)[agent.instructions].decode("utf-8")
        except UnicodeDecodeError:
            raise ResolveError(f"Instructions are not valid UTF-8: {agent.instructions}") from None

        profile_id = agent.runtime.model_profile
        if self.profiles.get_profile(profile_id) is None:
            raise ResolveError(f"Model profile not found: {profile_id}")
        try:
            profile_bytes = (self.profiles.root / f"{profile_id}.yaml").read_bytes()
            profile = ModelEndpointProfile.model_validate(yaml.safe_load(profile_bytes))
        except (OSError, yaml.YAMLError, ValidationError):
            raise ResolveError(f"Model profile unreadable or invalid: {profile_id}") from None

        sandbox, sandbox_digest = self._pin_sandbox(
            agent.runtime.sandbox_profile, required=any(t in SANDBOXED_TOOLS for t in agent.tools))

        mcp_manifests, mcp_raw = self._pin_mcp(agent)

        status = _git(base, "status", "--porcelain", "--", ".")
        files = {n: d for n, d in parts if n != "agent.yaml"}
        agent_yaml = dict(parts)["agent.yaml"]
        return PinnedDefinition(
            agent=agent,
            profile=profile,
            instructions=instructions,
            files=files,
            definition_bytes=agent_yaml,
            git_rev=_git(base, "rev-parse", "HEAD"),
            dirty=status is None or status != "",  # unknown git state counts as dirty
            definition_digest=compute_definition_digest(agent_yaml, files, mcp_raw),
            profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
            sandbox=sandbox,
            sandbox_digest=sandbox_digest,
            mcp_servers=mcp_manifests,
            mcp_bytes=mcp_raw,
        )
