from pathlib import Path

import yaml

from .models import (
    AgentDefinition,
    McpServerManifest,
    ModelEndpointProfile,
    SandboxProfile,
)
from .project import project_root


def agents_dir() -> Path:
    return project_root() / "agents"


def model_profiles_dir() -> Path:
    return project_root() / "model-profiles"


def sandbox_profiles_dir() -> Path:
    return project_root() / "sandbox-profiles"


def mcp_servers_dir() -> Path:
    return project_root() / "mcp-servers"


_LAZY = {
    "ROOT": project_root,
    "AGENTS_DIR": agents_dir,
    "MODEL_PROFILES_DIR": model_profiles_dir,
    "SANDBOX_PROFILES_DIR": sandbox_profiles_dir,
    "MCP_SERVERS_DIR": mcp_servers_dir,
}


def __getattr__(name: str) -> Path:
    # Old module constants (ROOT, AGENTS_DIR, ...) resolve the project when read, never at import.
    if name in _LAZY:
        return _LAZY[name]()
    raise AttributeError(name)


_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class ModelProfileRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root if root is not None else model_profiles_dir()).resolve()

    def list_profiles(self) -> list[ModelEndpointProfile]:
        if not self.root.exists():
            return []
        return [self._parse(p) for p in sorted(self.root.glob("*.yaml")) if not p.is_symlink()]

    def get_profile(self, profile_id: str) -> ModelEndpointProfile | None:
        if not profile_id or not set(profile_id) <= _ID_CHARS:
            return None
        path = self.root / f"{profile_id}.yaml"
        if not path.is_file() or path.is_symlink():
            return None
        profile = self._parse(path)
        return profile if profile.id == profile_id else None

    @staticmethod
    def _parse(path: Path) -> ModelEndpointProfile:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ModelEndpointProfile.model_validate(data)


class SandboxProfileRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root if root is not None else sandbox_profiles_dir()).resolve()

    def list_profiles(self) -> list[SandboxProfile]:
        if not self.root.exists():
            return []
        return [self._parse(p) for p in sorted(self.root.glob("*.yaml")) if not p.is_symlink()]

    def get_profile(self, profile_id: str) -> SandboxProfile | None:
        if not profile_id or not set(profile_id) <= _ID_CHARS:
            return None
        path = self.root / f"{profile_id}.yaml"
        if not path.is_file() or path.is_symlink():
            return None
        profile = self._parse(path)
        return profile if profile.id == profile_id else None

    @staticmethod
    def _parse(path: Path) -> SandboxProfile:
        return SandboxProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class McpServerRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root if root is not None else mcp_servers_dir()).resolve()

    def list_servers(self) -> list[McpServerManifest]:
        if not self.root.exists():
            return []
        return [self._parse(p.read_bytes()) for p in sorted(self.root.glob("*.yaml")) if not p.is_symlink()]

    def get_server(self, server_id: str) -> McpServerManifest | None:
        loaded = self.load(server_id)
        return loaded[0] if loaded else None

    def load(self, server_id: str) -> tuple[McpServerManifest, bytes] | None:
        """The manifest and the exact bytes it was parsed from (so a digest covers what is used)."""
        if not server_id or not set(server_id) <= _ID_CHARS:
            return None
        path = self.root / f"{server_id}.yaml"
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()
        manifest = self._parse(data)
        return (manifest, data) if manifest.id == server_id else None

    @staticmethod
    def _parse(data: bytes) -> McpServerManifest:
        return McpServerManifest.model_validate(yaml.safe_load(data.decode("utf-8")))


class AgentRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root if root is not None else agents_dir()).resolve()

    def list_agents(self) -> list[AgentDefinition]:
        agents: list[AgentDefinition] = []
        if not self.root.exists():
            return agents
        for definition in sorted(self.root.glob("*/agent.yaml")):
            agents.append(self._parse(definition))
        return agents

    def get_agent(self, agent_id: str) -> AgentDefinition | None:
        # Reject traversal; IDs are validated again against file content.
        if not agent_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in agent_id):
            return None
        path = self.root / agent_id / "agent.yaml"
        if not path.is_file() or path.is_symlink():
            return None
        agent = self._parse(path)
        return agent if agent.id == agent_id else None

    @staticmethod
    def _parse(path: Path) -> AgentDefinition:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return AgentDefinition.model_validate(data)
