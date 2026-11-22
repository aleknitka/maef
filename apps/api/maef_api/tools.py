"""Tools a model may call. Read-only built-ins only; results never raise and are size-capped."""
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .adapters.base import ToolSpec

# Single source of truth for valid `tools:` names in an agent definition.
BUILTIN_TOOL_NAMES = ("current_time", "read_agent_file")
MAX_RESULT_BYTES = 32 * 1024
_MARKER = "\n[truncated]"

_SPECS = {
    "current_time": ToolSpec("current_time", "Current UTC date and time as an ISO 8601 timestamp.",
                             {"type": "object", "properties": {}}),
    "read_agent_file": ToolSpec(
        "read_agent_file",
        "Read one of this agent's own files (its instructions or a listed skill) by relative path.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
}


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


class ToolSet(Protocol):
    def specs(self) -> list[ToolSpec]: ...

    def invoke(self, name: str, arguments: dict) -> ToolResult:
        """Never raises: an unknown tool or bad arguments yield an error result."""
        ...


def _cap(text: str) -> str:
    data = text.encode()
    if len(data) <= MAX_RESULT_BYTES:
        return text
    return data[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore") + _MARKER


class BuiltinTools:
    def __init__(self, names: list[str], files: Mapping[str, bytes],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        """`files` are the pinned instructions and skills (agent-relative path -> bytes); read_agent_file
        serves only these, so a run sees exactly what was digested and never touches the filesystem."""
        unknown = [n for n in names if n not in BUILTIN_TOOL_NAMES]
        if unknown:
            raise ValueError(f"Unknown builtin tool: {unknown[0]}")
        self._names = list(dict.fromkeys(names))
        self._clock = clock
        self._files = dict(files)

    def specs(self) -> list[ToolSpec]:
        return [_SPECS[n] for n in self._names]

    def invoke(self, name: str, arguments: dict) -> ToolResult:
        try:
            if name not in self._names:
                return ToolResult("unknown_tool", True)
            result = self._current_time() if name == "current_time" else self._read_agent_file(arguments)
            return ToolResult(_cap(result.content), result.is_error)
        except Exception:  # noqa: BLE001  # the contract is never to raise
            return ToolResult("tool_failed", True)

    def _current_time(self) -> ToolResult:
        return ToolResult(self._clock().astimezone(UTC).isoformat())

    def _read_agent_file(self, arguments: dict) -> ToolResult:
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if not isinstance(path, str) or not path:
            return ToolResult("invalid_arguments: path must be a string", True)
        rel = Path(path).as_posix()
        data = self._files.get(rel)
        if data is None:
            return ToolResult("file_not_available", True)
        return ToolResult(data.decode("utf-8", errors="replace"))


def spec_bytes(specs: list[ToolSpec]) -> int:
    """Size of the tool definitions sent with every call; they count toward the prompt."""
    if not specs:
        return 0
    return len(json.dumps([[s.name, s.description, s.input_schema] for s in specs]).encode())
