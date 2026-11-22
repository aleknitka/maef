"""MCP over stdio: a minimal JSON-RPC 2.0 client and the ToolSet built on it.

The server runs in its own sandbox container (see worker.py); this module only speaks newline-
delimited JSON-RPC over the `Process` the sandbox spawned. Everything a server sends is untrusted:
descriptions are sanitised and capped, schemas must be JSON objects, results are size-capped, and
no failure raises out of `McpToolSet.invoke`. Tool arguments and results are never logged.
"""
import itertools
import json
import re
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from typing import Any

from .adapters.base import ToolSpec
from .models import McpServerManifest, SandboxProfile
from .sandbox import MAX_LINE_BYTES, Process, SandboxError
from .tools import ToolResult, ToolSet, cap_text

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 30.0
MAX_DESCRIPTION_BYTES = 1024
MAX_LIST_PAGES = 100
# Per-server allowance added to the admission estimate: tool specs are unknown until the server runs.
ADMISSION_ALLOWANCE_BYTES = 8 * 1024
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class McpError(Exception):
    """Protocol or transport failure. The message is a short code, never server output."""


def mcp_tool_name(server_id: str, tool: str) -> str:
    return f"mcp__{server_id}__{tool}"


def sanitize_description(text: object) -> str:
    """Untrusted text: control and format characters removed (newline and tab kept), capped at 1 KiB."""
    if not isinstance(text, str):
        return ""
    clean = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co"))
    return clean.encode()[:MAX_DESCRIPTION_BYTES].decode("utf-8", errors="ignore")


def sandbox_profile_for(manifest: McpServerManifest, base: SandboxProfile | None) -> SandboxProfile:
    """The container config for a server: its own image, resource limits and user from the agent's
    sandbox profile when it has one, else conservative defaults. Network is passed separately."""
    return SandboxProfile(
        schema_version="maef.dev/v1alpha1", id=manifest.id, image=manifest.image,
        cpus=base.cpus if base else 1.0, memory_mb=base.memory_mb if base else 256,
        pids_limit=base.pids_limit if base else 128, workspace_mb=base.workspace_mb if base else 64,
        require_rootless=True if manifest.user == "0:0" else (base.require_rootless if base else True),
        user=manifest.user or (base.user if base else "65534:65534"))


class _Rpc:
    def __init__(self, process: Process):
        self._process = process
        self._ids = itertools.count(1)

    def _send(self, message: dict) -> None:
        try:
            self._process.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        except SandboxError:
            raise McpError("server_unreachable") from None

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: dict, timeout: float) -> dict:
        request_id = next(self._ids)
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError("timeout")
            line = self._process.readline(remaining)
            if line is None:
                raise McpError("timeout")
            if line == b"":
                raise McpError("server_exited")
            if len(line) > MAX_LINE_BYTES:
                raise McpError("line_too_long")
            if not line.strip():
                continue
            # Some servers log to stdout; skip non-JSON-RPC lines (the deadline still bounds the wait).
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            # Notifications, server-initiated requests and stale answers are not ours: ignore.
            if "method" in message or message.get("id") != request_id or isinstance(message.get("id"), bool):
                continue
            if "error" in message:
                raise McpError("rpc_error")
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError("malformed_result")
            return result


class McpToolSet:
    """The tools of one MCP server, exposed as `mcp__<server_id>__<tool>`. Call `start()` once."""

    def __init__(self, name: str, process: Process, *, max_tools: int = 32,
                 timeout: Callable[[], float] = lambda: DEFAULT_TIMEOUT):
        self.name, self._process, self._max_tools, self._timeout = name, process, max_tools, timeout
        self._rpc = _Rpc(process)
        self._tools: dict[str, str] = {}  # exposed name -> server's tool name
        self._specs: list[ToolSpec] = []
        self._dead = False
        self.listed = 0  # tools the server offered
        self.truncated = False

    def start(self) -> "McpToolSet":
        """Handshake and list tools; raises McpError. A failed start leaves the set unusable."""
        try:
            info = self._rpc.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "maef", "version": "0.1.0"}}, self._budget())
            if not isinstance(info.get("protocolVersion"), str):
                raise McpError("malformed_result")
            self._rpc.notify("notifications/initialized")
            self._list_tools()
        except McpError:
            self._dead = True
            raise
        return self

    def _budget(self) -> float:
        return max(min(DEFAULT_TIMEOUT, self._timeout()), 0.0)

    def _list_tools(self) -> None:
        raw: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = self._rpc.request("tools/list", {"cursor": cursor} if cursor else {}, self._budget())
            tools = page.get("tools")
            if not isinstance(tools, list):
                raise McpError("malformed_result")
            raw.extend(tools)
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or len(raw) > self._max_tools:
                break
        self.listed = len(raw)
        self.truncated = len(raw) > self._max_tools
        for tool in raw[: self._max_tools]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            tool_name, schema = tool["name"], tool.get("inputSchema")
            exposed = mcp_tool_name(self.name, tool_name)
            if not _TOOL_NAME.fullmatch(tool_name) or not isinstance(schema, dict) or exposed in self._tools:
                continue
            self._tools[exposed] = tool_name
            self._specs.append(ToolSpec(exposed, sanitize_description(tool.get("description")), schema))

    # -- ToolSet --
    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def server_of(self, name: str) -> str | None:
        return self.name if name in self._tools else None

    def invoke(self, name: str, arguments: dict) -> ToolResult:
        try:
            tool = self._tools.get(name)
            if tool is None:
                return ToolResult("unknown_tool", True)
            if not isinstance(arguments, dict):
                return ToolResult("invalid_arguments: arguments must be an object", True)
            if self._dead:
                return ToolResult("mcp_server_unavailable", True)
            timeout = self._budget()
            if timeout <= 0:
                return ToolResult("duration_exhausted", True)
            try:
                result = self._rpc.request("tools/call", {"name": tool, "arguments": arguments}, timeout)
            except McpError as exc:
                if str(exc) != "timeout" and str(exc) != "rpc_error":
                    self._dead = True  # transport is broken; later calls fail fast
                return ToolResult(f"mcp_error:{exc}", True)
            return ToolResult(cap_text(_render(result.get("content"))), result.get("isError") is True)
        except Exception:  # noqa: BLE001  # the contract is never to raise
            return ToolResult("tool_failed", True)

    def close(self) -> None:
        self._process.close()


def _render(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        else:
            kind = item.get("type") if isinstance(item, dict) else None
            kind = kind if isinstance(kind, str) else "unknown"
            parts.append(f"[{sanitize_description(kind)[:32] or 'unknown'} omitted]")
    return "\n".join(parts)


class CompositeToolSet:
    """Merges tool sets; routes each call by tool name. Duplicate names are a construction error."""

    def __init__(self, sets: list[ToolSet]):
        self._sets = list(sets)
        self._owner: dict[str, ToolSet] = {}
        for tool_set in self._sets:
            for spec in tool_set.specs():
                if spec.name in self._owner:
                    raise ValueError(f"duplicate_tool_name:{spec.name}")
                self._owner[spec.name] = tool_set

    def specs(self) -> list[ToolSpec]:
        return [spec for tool_set in self._sets for spec in tool_set.specs()]

    def server_of(self, name: str) -> str | None:
        owner = self._owner.get(name)
        server_of = getattr(owner, "server_of", None)
        return server_of(name) if server_of else None

    def invoke(self, name: str, arguments: dict) -> ToolResult:
        owner = self._owner.get(name)
        return owner.invoke(name, arguments) if owner else ToolResult("unknown_tool", True)


# --- test adapter ---

class FakeMcpServer:
    """In-memory `Process` that answers MCP JSON-RPC. `tools` are tools/list entries; `call(name,
    arguments)` returns the tools/call result dict. `page_size` paginates tools/list. Everything
    the client sent is kept in `received`; `junk` lines are emitted before each response."""

    def __init__(self, tools: list[dict] | None = None,
                 call: Callable[[str, dict], dict] | None = None, *,
                 page_size: int | None = None, junk: list[bytes] | None = None):
        self.tools = tools or []
        self._call = call or (lambda name, arguments: {"content": [{"type": "text", "text": "ok"}]})
        self.page_size, self.junk = page_size, junk or []
        self.received: list[dict] = []
        self.closed = False
        self.silent = False  # when True, requests go unanswered (timeout tests)
        self._out: deque[bytes] = deque()

    def write(self, data: bytes) -> None:
        if self.closed:
            raise SandboxError("process_write_failed")
        message = json.loads(data)
        self.received.append(message)
        if "id" not in message or self.silent:
            return
        self._out.extend(self.junk)
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "tools/list":
            start = int(params.get("cursor") or 0)
            size = self.page_size or len(self.tools) or 1
            result = {"tools": self.tools[start:start + size]}
            if start + size < len(self.tools):
                result["nextCursor"] = str(start + size)
        elif method == "tools/call":
            result = self._call(params["name"], params["arguments"])
        else:
            self._out.append(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                         "error": {"code": -32601, "message": "no"}}).encode() + b"\n")
            return
        self._out.append(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode() + b"\n")

    def readline(self, timeout: float) -> bytes | None:
        if self._out:
            return self._out.popleft()
        return b"" if self.closed else None

    def close(self) -> None:
        self.closed = True
