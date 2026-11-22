"""Pin an agent definition to exact content so a run can be reproduced and audited.

Every file is read once; the same bytes are digested and used, so what ran is what was pinned.
"""
import hashlib
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import AgentDefinition, ModelEndpointProfile
from .registry import AgentRegistry, ModelProfileRegistry
from .tools import BUILTIN_TOOL_NAMES


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


def compute_definition_digest(agent_yaml: bytes, files: Mapping[str, bytes]) -> str:
    """The digest recorded at admission: agent.yaml first, then `files` in pinned order.
    Workers recompute it over the bytes they were handed."""
    return _digest([("agent.yaml", agent_yaml), *files.items()])


class Definitions:
    def __init__(self, agents: AgentRegistry, profiles: ModelProfileRegistry):
        self.agents = agents
        self.profiles = profiles

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
            definition_digest=compute_definition_digest(agent_yaml, files),
            profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
        )
