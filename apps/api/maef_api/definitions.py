"""Pin an agent definition to exact content so a run can be reproduced and audited.

Every file is read once; the same bytes are digested and used, so what ran is what was pinned.
"""
import hashlib
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gitrepo import GitError, GitRepo, safe_relpath
from .models import (
    AgentDefinition,
    McpServerManifest,
    ModelEndpointProfile,
    SandboxProfile,
)
from .registry import (
    _ID_CHARS,
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
    if any(m.network != "none" for m in manifests) and not agent.permissions.network_open:
        return "network_not_permitted"
    return None


class _GitSource:
    """Definition files read from one commit's object store instead of the working tree."""

    def __init__(self, repo: GitRepo, sha: str, agents: str, sandboxes: str | None, mcps: str | None):
        self.repo, self.sha = repo, sha
        self._prefix = {"agents": agents, "sandboxes": sandboxes, "mcps": mcps}

    def _read(self, kind: str, rel: str) -> bytes | None:
        prefix = self._prefix[kind]
        if prefix is None:
            return None
        try:
            return self.repo.read_blob(self.sha, f"{prefix}/{safe_relpath(rel)}" if prefix else safe_relpath(rel))
        except (GitError, ValueError) as exc:
            raise ResolveError(f"Cannot read {rel} at {self.sha[:12]}: {exc}") from None

    def agent(self, agent_id: str, names_for, check_tools) -> tuple[AgentDefinition, list[tuple[str, bytes]]]:
        if not agent_id or not set(agent_id) <= _ID_CHARS:
            raise ResolveError(f"Unknown agent: {agent_id}")
        raw = self._read("agents", f"{agent_id}/agent.yaml")
        if raw is None:
            raise ResolveError(f"Unknown agent: {agent_id} at {self.sha[:12]}")
        try:
            agent = AgentDefinition.model_validate(yaml.safe_load(raw))
        except (yaml.YAMLError, ValidationError):
            raise ResolveError(f"Agent definition unreadable or invalid at {self.sha[:12]}: {agent_id}") from None
        if agent.id != agent_id:
            raise ResolveError(f"Unknown agent: {agent_id} at {self.sha[:12]}")
        check_tools(agent)
        parts = [("agent.yaml", raw)]
        for name in names_for(agent)[1:]:
            data = None
            try:
                data = self._read("agents", f"{agent_id}/{name}")
            except ResolveError:
                pass
            if data is None:
                raise ResolveError(f"Agent file missing or outside the agent directory: {name}")
            parts.append((name, data))
        return agent, parts

    def sandbox(self, profile_id: str) -> bytes | None:
        if not profile_id or not set(profile_id) <= _ID_CHARS:
            return None
        return self._read("sandboxes", f"{profile_id}.yaml")

    def mcp(self, server_id: str) -> tuple[McpServerManifest, bytes] | None:
        if not server_id or not set(server_id) <= _ID_CHARS:
            return None
        data = self._read("mcps", f"{server_id}.yaml")
        if data is None:
            return None
        manifest = McpServerRegistry._parse(data)
        return (manifest, data) if manifest.id == server_id else None


class Definitions:
    def __init__(self, agents: AgentRegistry, profiles: ModelProfileRegistry,
                 sandboxes: SandboxProfileRegistry | None = None, mcps: McpServerRegistry | None = None,
                 repo_root: Path | None = None):
        self.agents = agents
        self.profiles = profiles
        self.sandboxes = sandboxes
        self.mcps = mcps
        # Where git objects are read for a pinned revision; by convention the parent of `agents/`.
        self.repo_root = Path(repo_root) if repo_root is not None else agents.root.parent

    def _git_source(self, revision: str) -> _GitSource:
        """Resolve `revision` (sha, branch or tag) now; ResolveError if it names no commit."""
        try:
            repo = GitRepo(self.repo_root)
            top = repo.toplevel()
            sha = repo.resolve_revision(revision)
            def prefix(root: Path | None) -> str | None:
                return None if root is None else (root.relative_to(top).as_posix() if root != top else "")
            return _GitSource(repo, sha, prefix(self.agents.root) or "",
                              prefix(self.sandboxes.root) if self.sandboxes else None,
                              prefix(self.mcps.root) if self.mcps else None)
        except (GitError, ValueError) as exc:
            raise ResolveError(f"Unknown revision: {revision[:80]} ({exc})") from None

    def _pin_mcp(self, agent: AgentDefinition, src: _GitSource | None = None) -> tuple[list[McpServerManifest], dict[str, bytes]]:
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
                if src is not None:
                    loaded = src.mcp(server_id)
                else:
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
            raise ResolveError("MCP server needs network access (allowlist or open) but the agent does not set permissions.network_open")
        return manifests, raw

    def _pin_sandbox(self, profile_id: str, required: bool, src: _GitSource | None = None) -> tuple[SandboxProfile | None, str | None]:
        try:
            if src is not None:
                data = src.sandbox(profile_id)
                if data is None:
                    raise ResolveError(f"Sandbox profile not found: {profile_id}")
                if SandboxProfile.model_validate(yaml.safe_load(data)).id != profile_id:
                    raise ResolveError(f"Sandbox profile not found: {profile_id}")
            else:
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

    def resolve_profile(self, profile_id: str) -> tuple[ModelEndpointProfile, bytes, str]:
        """A model profile pinned to its exact bytes: (profile, bytes, sha256 hex digest)."""
        if self.profiles.get_profile(profile_id) is None:
            raise ResolveError(f"Model profile not found: {profile_id}")
        try:
            data = (self.profiles.root / f"{profile_id}.yaml").read_bytes()
            profile = ModelEndpointProfile.model_validate(yaml.safe_load(data))
        except (OSError, yaml.YAMLError, ValidationError):
            raise ResolveError(f"Model profile unreadable or invalid: {profile_id}") from None
        return profile, data, hashlib.sha256(data).hexdigest()

    def resolve(self, agent_id: str, revision: str | None = None) -> PinnedDefinition:
        """Pin an agent. With `revision` (40-hex sha, branch or tag), every definition file comes from
        that commit's object store, `git_rev` is the commit and `dirty` is False; model profiles stay local."""
        src = self._git_source(revision) if revision is not None else None
        if src is None:
            agent = self.agents.get_agent(agent_id)
            if agent is None:
                raise ResolveError(f"Unknown agent: {agent_id}")

        def check_tools(a: AgentDefinition) -> None:
            for tool in a.tools:
                if tool not in BUILTIN_TOOL_NAMES:
                    raise ResolveError(f"Unknown tool: {tool}")

        def names_for(a: AgentDefinition) -> list[str]:
            return ["agent.yaml", a.instructions, *sorted(a.skills)]

        parts: list[tuple[str, bytes]] = []
        if src is not None:
            agent, parts = src.agent(agent_id, names_for, check_tools)
        else:
            check_tools(agent)
            # Only files inside the agent directory may be pinned; symlinks could escape it.
            base = (self.agents.root / agent_id).resolve()
            for name in names_for(agent):
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
            agent.runtime.sandbox_profile, required=any(t in SANDBOXED_TOOLS for t in agent.tools), src=src)

        mcp_manifests, mcp_raw = self._pin_mcp(agent, src)

        if src is None:
            # Dirty covers everything pinned from the working tree: the agent directory plus the
            # MCP manifests and sandbox profile it references (model profiles are local, not in git).
            specs = ["."]
            if self.mcps is not None:
                specs += [str(self.mcps.root / f"{sid}.yaml") for sid in sorted(mcp_raw or {})]
            if sandbox is not None and self.sandboxes is not None:
                specs.append(str(self.sandboxes.root / f"{agent.runtime.sandbox_profile}.yaml"))
            status = _git(base, "status", "--porcelain", "--", *specs)
            git_rev, dirty = _git(base, "rev-parse", "HEAD"), status is None or status != ""  # unknown git state counts as dirty
        else:
            git_rev, dirty = src.sha, False
        files = {n: d for n, d in parts if n != "agent.yaml"}
        agent_yaml = dict(parts)["agent.yaml"]
        return PinnedDefinition(
            agent=agent,
            profile=profile,
            instructions=instructions,
            files=files,
            definition_bytes=agent_yaml,
            git_rev=git_rev,
            dirty=dirty,
            definition_digest=compute_definition_digest(agent_yaml, files, mcp_raw),
            profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
            sandbox=sandbox,
            sandbox_digest=sandbox_digest,
            mcp_servers=mcp_manifests,
            mcp_bytes=mcp_raw,
        )
