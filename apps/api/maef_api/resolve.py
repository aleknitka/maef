"""Pin an agent definition to exact content so a run can be reproduced and audited."""
import hashlib
import subprocess
from pathlib import Path

from .models import ResolvedDefinition
from .registry import AgentRegistry, ModelProfileRegistry


class ResolveError(Exception):
    """The definition cannot be pinned (unknown agent, profile or unreadable file)."""


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


def resolve_definition(
    agent_id: str,
    agents: AgentRegistry,
    profiles: ModelProfileRegistry,
) -> ResolvedDefinition:
    agent = agents.get_agent(agent_id)
    if agent is None:
        raise ResolveError(f"Unknown agent: {agent_id}")
    base = (agents.root / agent_id).resolve()

    # Only files inside the agent directory may be pinned; symlinks could escape it.
    names = ["agent.yaml", agent.instructions, *sorted(agent.skills)]
    parts: list[tuple[str, bytes]] = []
    for name in names:
        path = base / name
        if path.is_symlink() or not path.resolve().is_relative_to(base) or not path.is_file():
            raise ResolveError(f"Agent file missing or outside the agent directory: {name}")
        parts.append((name, path.read_bytes()))

    profile_id = agent.runtime.model_profile
    profile = profiles.get_profile(profile_id)
    if profile is None:
        raise ResolveError(f"Model profile not found: {profile_id}")
    profile_bytes = (profiles.root / f"{profile_id}.yaml").read_bytes()

    git_rev = _git(base, "rev-parse", "HEAD")
    status = _git(base, "status", "--porcelain", "--", ".")
    return ResolvedDefinition(
        agent_id=agent.id,
        agent_version=agent.version,
        git_rev=git_rev,
        dirty=status is None or status != "",  # unknown git state counts as dirty
        definition_digest=_digest(parts),
        model_profile_id=profile_id,
        profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
    )
