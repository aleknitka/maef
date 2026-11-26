"""Tools a model may call. Read-only built-ins only; results never raise and are size-capped."""

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .adapters.base import ToolSpec
from .sandbox import SandboxError, Session

# Single source of truth for valid `tools:` names in an agent definition.
BUILTIN_TOOL_NAMES = ("current_time", "read_agent_file", "run_command")
# Tools that execute in a sandbox; an agent listing any of them needs a sandbox profile.
SANDBOXED_TOOLS = frozenset({"run_command"})
DEFAULT_EXEC_TIMEOUT = 30.0
MAX_RESULT_BYTES = 32 * 1024
_STREAM_CAP = 12 * 1024
_CAP_HEADROOM = 4 * 1024  # read a little past the model-visible cap so truncation is detected
_CAP_HEADROOM = 4 * 1024  # read a little past the model-visible cap so truncation is detected
_MARKER = "\n[truncated]"

_SPECS = {
    "current_time": ToolSpec(
        "current_time", "Current UTC date and time as an ISO 8601 timestamp.", {"type": "object", "properties": {}}
    ),
    "read_agent_file": ToolSpec(
        "read_agent_file",
        "Read one of this agent's own files (its instructions or a listed skill) by relative path.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    "run_command": ToolSpec(
        "run_command",
        "Run a command in an isolated sandbox with no network. Working directory /workspace is "
        "writable; everything else is read-only. Returns JSON {exit_code, stdout, stderr, timed_out}.",
        {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "stdin": {"type": "string"},
            },
            "required": ["argv"],
        },
    ),
}


def builtin_specs(names: list[str]) -> list[ToolSpec]:
    """Specs for `names` without constructing tools (used at admission); ValueError on unknown names."""
    unknown = [n for n in names if n not in BUILTIN_TOOL_NAMES]
    if unknown:
        raise ValueError(f"Unknown builtin tool: {unknown[0]}")
    return [_SPECS[n] for n in dict.fromkeys(names)]


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


class ToolSet(Protocol):
    def specs(self) -> list[ToolSpec]: ...

    def invoke(self, name: str, arguments: dict) -> ToolResult:
        """Never raises: an unknown tool or bad arguments yield an error result."""
        ...


def cap_text(text: str) -> str:
    data = text.encode()
    if len(data) <= MAX_RESULT_BYTES:
        return text
    return data[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore") + _MARKER


class BuiltinTools:
    def __init__(
        self,
        names: list[str],
        files: Mapping[str, bytes],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        session: Session | None = None,
        exec_timeout: Callable[[], float] = lambda: DEFAULT_EXEC_TIMEOUT,
        cancel: threading.Event | None = None,
    ):
        """`files` are the pinned instructions and skills (agent-relative path -> bytes); read_agent_file
        serves only these, so a run sees exactly what was digested and never touches the filesystem."""
        builtin_specs(names)  # validates
        if session is None and any(n in SANDBOXED_TOOLS for n in names):
            raise ValueError("Sandboxed tools require a sandbox session")
        self._session, self._exec_timeout, self._cancel = session, exec_timeout, cancel
        self._names = list(dict.fromkeys(names))
        self._clock = clock
        self._files = dict(files)

    def specs(self) -> list[ToolSpec]:
        return [_SPECS[n] for n in self._names]

    def server_of(self, name: str) -> str | None:
        return None

    def invoke(self, name: str, arguments: dict) -> ToolResult:
        try:
            if name not in self._names:
                return ToolResult("unknown_tool", True)
            if name == "current_time":
                result = self._current_time()
            elif name == "run_command":
                result = self._run_command(arguments)
            else:
                result = self._read_agent_file(arguments)
            return ToolResult(cap_text(result.content), result.is_error)
        except Exception:  # noqa: BLE001  # the contract is never to raise
            return ToolResult("tool_failed", True)

    def _current_time(self) -> ToolResult:
        return ToolResult(self._clock().astimezone(UTC).isoformat())

    def _run_command(self, arguments: dict) -> ToolResult:
        argv = arguments.get("argv") if isinstance(arguments, dict) else None
        stdin = arguments.get("stdin") if isinstance(arguments, dict) else None
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) for a in argv)
            or not (stdin is None or isinstance(stdin, str))
        ):
            return ToolResult("invalid_arguments: argv must be a non-empty list of strings", True)
        timeout = min(DEFAULT_EXEC_TIMEOUT, self._exec_timeout())
        if timeout <= 0:
            return ToolResult("duration_exhausted", True)
        if self._session is None:  # unreachable: __init__ requires a session for sandboxed tools
            return ToolResult("sandbox_error", True)
        try:
            extra = {"cancel": self._cancel} if self._cancel is not None else {}
            res = self._session.exec(
                argv,
                stdin=stdin.encode() if stdin is not None else None,
                timeout=timeout,
                max_bytes=_STREAM_CAP + _CAP_HEADROOM,
                **extra,
            )
        except (SandboxError, ValueError):
            return ToolResult("sandbox_error", True)
        # Each stream is capped so the JSON stays valid after the overall result cap.
        body = {
            "exit_code": res.exit_code,
            "stdout": res.stdout[:_STREAM_CAP].decode("utf-8", errors="replace"),
            "stderr": res.stderr[:_STREAM_CAP].decode("utf-8", errors="replace"),
            "timed_out": res.timed_out,
        }
        if res.truncated:
            body["truncated"] = True
            body["note"] = (
                f"output truncated at {_STREAM_CAP} bytes per stream; command stopped"
                f" (at least {res.discarded_bytes} more bytes were discarded)"
            )
        elif max(len(res.stdout), len(res.stderr)) > _STREAM_CAP:
            body["truncated"] = True
            body["note"] = f"output truncated at {_STREAM_CAP} bytes per stream"
        if res.truncated:
            body["truncated"] = True
            body["note"] = (
                f"output truncated at {_STREAM_CAP} bytes per stream; command stopped"
                f" (at least {res.discarded_bytes} more bytes were discarded)"
            )
        if res.cancelled:
            body["cancelled"] = True
        return ToolResult(json.dumps(body), res.timed_out or res.cancelled)

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
