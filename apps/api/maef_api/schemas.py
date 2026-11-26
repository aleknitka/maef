"""JSON Schema for the definition formats, exported from the pydantic models (the single source of truth).

`schemas/<name>.schema.json` is committed; `check_schemas` reports drift so CI can fail a model change
that was not re-exported.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from .models import (
    AgentDefinition,
    McpServerManifest,
    ModelEndpointProfile,
    SandboxProfile,
)
from .registry import ROOT

SCHEMAS_DIR = ROOT / "schemas"
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "agent-definition": AgentDefinition,
    "model-endpoint-profile": ModelEndpointProfile,
    "sandbox-profile": SandboxProfile,
    "mcp-server-manifest": McpServerManifest,
}


def schema_for(name: str) -> dict | None:
    model = SCHEMA_MODELS.get(name)
    return model.model_json_schema() if model else None


def schema_text(name: str) -> str:
    """Canonical file content: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(schema_for(name), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _path(directory: Path, name: str) -> Path:
    return Path(directory) / f"{name}.schema.json"


def write_schemas(directory: Path = SCHEMAS_DIR) -> list[Path]:
    Path(directory).mkdir(parents=True, exist_ok=True)
    written = []
    for name in SCHEMA_MODELS:
        path = _path(directory, name)
        path.write_text(schema_text(name), encoding="utf-8")
        written.append(path)
    return written


def check_schemas(directory: Path = SCHEMAS_DIR) -> list[str]:
    """Names whose committed file is missing or differs from the models."""
    drift = []
    for name in SCHEMA_MODELS:
        path = _path(directory, name)
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != schema_text(name):
            drift.append(name)
    return drift
