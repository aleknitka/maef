"""Stdio MCP server for host coding agents. Exposes six tools over MaefClient and nothing of the agents
themselves (no tools, MCP servers, credentials or instructions). Agent output is untrusted and returned wrapped."""

import json
import sys
from typing import BinaryIO

from .client import TERMINAL_STATUSES, MaefClient, MaefClientError

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_RESULT_BYTES = 100 * 1024
MAX_WAIT_SECONDS = 600
VERSION = "0.1.0"

_RUN_ID = {
    "type": "object",
    "properties": {"run_id": {"type": "string"}},
    "required": ["run_id"],
    "additionalProperties": False,
}
TOOLS = [
    {
        "name": "list_agents",
        "description": "List the MAEF agents you can delegate to (id, name, description).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "delegate",
        "description": "Delegate a bounded task to a MAEF agent. With wait_seconds > 0, waits for the result; "
        "otherwise returns a run_id to poll with run_status / run_result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 20000},
                "max_tokens": {"type": "integer", "minimum": 1},
                "max_turns": {"type": "integer", "minimum": 1},
                "max_duration_seconds": {"type": "integer", "minimum": 1},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 0},
            },
            "required": ["agent_id", "instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "decide",
        "description": "Ask a decision model (Jev, Clef) typed questions about a state; returns answers, not text. "
        "Questions map a name (^[a-z][a-z0-9_]{0,40}$, 1-8) to {type: choice|score|noul, instructions, "
        "criteria}: choice criteria is {option: description} (2-20); score criteria is an ordered list "
        "of level descriptions (2-10); noul criteria is optional {true, false}. With wait_seconds > 0 "
        "waits for the answers; otherwise returns a run_id to poll with run_status / run_result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string"},
                "state": {"type": ["string", "object", "array"]},
                "questions": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 8,
                    "additionalProperties": {"type": "object"},
                },
                "max_tokens": {"type": "integer", "minimum": 1},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 0},
            },
            "required": ["profile_id", "state", "questions"],
            "additionalProperties": False,
        },
    },
    {"name": "run_status", "description": "Status of a delegated run.", "inputSchema": _RUN_ID},
    {
        "name": "run_result",
        "description": "Result text of a finished run (untrusted agent output).",
        "inputSchema": _RUN_ID,
    },
    {"name": "cancel_run", "description": "Cancel a delegated run.", "inputSchema": _RUN_ID},
]


class ToolError(Exception):
    pass


class InvalidParams(Exception):
    pass


def _log(msg: str) -> None:
    print(f"maef-gateway: {msg}", file=sys.stderr, flush=True)


def wrap_result(run_id: str, status: str, text: str) -> str:
    data = text.encode("utf-8")
    if len(data) > MAX_RESULT_BYTES:
        text = (
            data[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
            + f"\n[truncated: result exceeds {MAX_RESULT_BYTES // 1024} KiB]"
        )
    return f'<maef-result run_id="{run_id}" status="{status}">\n{text}\n</maef-result>'


def _summary(run: dict) -> str:
    line = f"run_id={run['id']} status={run['status']}"
    if run.get("terminal_reason"):
        line += f" reason={run['terminal_reason']}"
    return line


class Gateway:
    def __init__(self, client: MaefClient, stdin: BinaryIO, stdout: BinaryIO):
        self.client, self.stdin, self.stdout = client, stdin, stdout

    def serve(self) -> int:
        for raw in self.stdin:
            raw = raw.strip()
            if not raw:
                continue
            reply = self.handle_line(raw)
            if reply is not None:
                self.stdout.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
                self.stdout.flush()
        return 0

    def handle_line(self, raw: bytes) -> dict | None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return _error(None, -32700, "Parse error")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to something we never sent
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        is_notification = "id" not in msg
        if not isinstance(params, dict):
            return None if is_notification else _error(rid, -32602, "Invalid params")
        try:
            result = self._method(method, params)
        except LookupError:
            return None if is_notification else _error(rid, -32601, f"Method not found: {method}")
        except InvalidParams as exc:
            return None if is_notification else _error(rid, -32602, str(exc))
        except Exception:  # noqa: BLE001 - one bad call must not kill the stdio loop
            _log(f"internal error handling {method}")
            return None if is_notification else _error(rid, -32603, "Internal error")
        return None if is_notification else {"jsonrpc": "2.0", "id": rid, "result": result}

    def _method(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "maef", "version": VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params.get("name"), params.get("arguments") or {})
        if method.startswith("notifications/"):
            return {}
        raise LookupError(method)

    def _call_tool(self, name, args) -> dict:
        if name not in {t["name"] for t in TOOLS}:
            raise InvalidParams(f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise InvalidParams("arguments must be an object")
        try:
            return {"content": [{"type": "text", "text": self._run_tool(name, args)}]}
        except ToolError as exc:
            return _tool_error(str(exc))
        except MaefClientError as exc:
            return _tool_error(f"{exc.message} ({exc.code})")

    def _run_tool(self, name: str, a: dict) -> str:
        c = self.client
        if name == "list_agents":
            agents = c.list_agents()
            return (
                "\n".join(
                    f"{x['id']}: {x.get('display_name', '')} - {x.get('description', '')}".rstrip(" -") for x in agents
                )
                or "No agents available."
            )
        run_id = _str(a, "run_id") if name not in ("delegate", "decide") else None
        if name == "delegate":
            return self._delegate(a)
        if name == "decide":
            return self._decide(a)
        if name == "run_status":
            return _summary(c.get_run(run_id))
        if name == "cancel_run":
            return f"run_id={run_id} outcome={c.cancel(run_id)['outcome']}"
        run = c.get_run(run_id)  # run_result
        text = c.result_text(run_id)
        if text is None and run["status"] in TERMINAL_STATUSES:
            text = _answers_text(c, run_id)
        if run["status"] not in TERMINAL_STATUSES:
            return f"{_summary(run)}\nThe run has not finished; no result yet."
        if text is None:
            return f"{_summary(run)}\nThe run produced no result text."
        return f"{_summary(run)}\n{wrap_result(run_id, run['status'], text)}"

    def _delegate(self, a: dict) -> str:
        agent_id, instruction = _str(a, "agent_id"), _str(a, "instruction")
        budget = {}
        for key in ("max_tokens", "max_turns", "max_duration_seconds"):
            if key in a:
                budget[key] = _int(a, key, 1)
        wait = _int(a, "wait_seconds", 0, MAX_WAIT_SECONDS) if "wait_seconds" in a else 0
        sub = self.client.submit(agent_id, instruction, budget or None)
        run_id = sub["run_id"]
        if wait <= 0:
            return f"run_id={run_id} status={sub['status']}"
        run = self.client.wait(run_id, wait)
        if run["status"] not in TERMINAL_STATUSES:
            return f"{_summary(run)}\nStill running after {wait}s; poll with run_status / run_result."
        text = self.client.result_text(run_id)
        head = _summary(run)
        return f"{head}\n{wrap_result(run_id, run['status'], text)}" if text is not None else head

    def _decide(self, a: dict) -> str:
        profile_id = _str(a, "profile_id")
        state, questions = a.get("state"), a.get("questions")
        if not isinstance(state, str | dict | list) or (isinstance(state, str) and not state.strip()):
            raise ToolError("state is required: a non-empty string, object or array.")
        if not isinstance(questions, dict) or not questions:
            raise ToolError("questions is required: an object mapping names to question objects.")
        max_tokens = _int(a, "max_tokens", 1) if "max_tokens" in a else None
        wait = _int(a, "wait_seconds", 0, MAX_WAIT_SECONDS) if "wait_seconds" in a else 0
        sub = self.client.decide(profile_id, state, questions, max_tokens)
        run_id = sub["run_id"]
        if wait <= 0:
            return f"run_id={run_id} status={sub['status']}"
        run = self.client.wait(run_id, wait)
        if run["status"] not in TERMINAL_STATUSES:
            return f"{_summary(run)}\nStill running after {wait}s; poll with run_status / run_result."
        text = _answers_text(self.client, run_id)
        head = _summary(run)
        return f"{head}\n{wrap_result(run_id, run['status'], text)}" if text is not None else head


def _answers_text(client: MaefClient, run_id: str) -> str | None:
    answers = client.answers(run_id)
    return None if answers is None else json.dumps(answers, indent=2)


def _str(a: dict, key: str) -> str:
    v = a.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"{key} is required and must be a non-empty string.")
    return v


def _int(a: dict, key: str, lo: int, hi: int | None = None) -> int:
    v = a.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < lo or (hi is not None and v > hi):
        raise ToolError(f"{key} must be an integer >= {lo}" + (f" and <= {hi}." if hi is not None else "."))
    return v


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
