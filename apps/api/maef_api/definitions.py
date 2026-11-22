"""Pin an agent definition to exact content so a run can be reproduced and audited.

Every file is read once; the same bytes are digested and used, so what ran is what was pinned.
"""
import hashlib
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .models import AgentDefinition, ModelEndpointProfile
from .registry import AgentRegistry, ModelProfileRegistry


class ResolveError(Exception):
    """The definition cannot be pinned (unknown agent, profile or unreadable file)."""


class PinnedDefinition(BaseModel):
    """An agent and its model profile pinned to exact content, ready for admission."""
    model_config = ConfigDict(frozen=True)
    agent: AgentDefinition
    profile: ModelEndpointProfile
    # The system prompt, decoded from the exact bytes included in definition_digest.
    instructions: str
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


class Definitions:
    def __init__(self, agents: AgentRegistry, profiles: ModelProfileRegistry):
        self.agents = agents
        self.profiles = profiles

    def resolve(self, agent_id: str) -> PinnedDefinition:
        agent = self.agents.get_agent(agent_id)
        if agent is None:
            raise ResolveError(f"Unknown agent: {agent_id}")
        base = (self.agents.root / agent_id).resolve()

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
        return PinnedDefinition(
            agent=agent,
            profile=profile,
            instructions=instructions,
            git_rev=_git(base, "rev-parse", "HEAD"),
            dirty=status is None or status != "",  # unknown git state counts as dirty
            definition_digest=_digest(parts),
            profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
        )
