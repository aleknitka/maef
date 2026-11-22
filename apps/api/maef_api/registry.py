from pathlib import Path
import yaml
from .models import AgentDefinition, ModelEndpointProfile

ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = ROOT / "agents"
MODEL_PROFILES_DIR = ROOT / "model-profiles"
_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")

class ModelProfileRegistry:
    def __init__(self, root: Path = MODEL_PROFILES_DIR):
        self.root = Path(root).resolve()

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

class AgentRegistry:
    def __init__(self, root: Path = AGENTS_DIR):
        self.root = Path(root).resolve()

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
