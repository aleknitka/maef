"""MaefClient: one typed-by-convention client over the public run API, shared by the CLI and the MCP gateway.

Interface: `MaefClient(transport)` plus the methods below. Callers never see HTTP, SSE framing, reconnection or
error-body parsing. The seam is `Transport` (two methods); `HttpTransport` is the production adapter and
`AsgiTransport` drives an in-process FastAPI app for tests. Error text never includes more than the API's own
`detail` message (truncated), so prompts or keys echoed in other bodies cannot leak.
"""
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import quote, urlparse

from .adapters.http import opener
from .egress import EgressDenied, EgressPolicy

DEFAULT_URL = "http://127.0.0.1:8000"
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
TERMINAL_EVENTS = ("task.succeeded", "task.failed", "task.cancelled")
RESULT_NAME = "output.md"
ANSWERS_NAME = "answers.json"
_MAX_MESSAGE = 300
AUTH_HINT = "Set MAEF_API_TOKEN (create one with `maef admin token create`)."
_MAX_RECONNECTS = 3


class MaefClientError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def __str__(self) -> str:
        return f"{self.message} (HTTP {self.status}, {self.code})" if self.status else f"{self.message} ({self.code})"


def error_from_response(status: int, body: bytes) -> MaefClientError:
    """Build an error from the API's JSON `detail` only; anything else in the body is dropped."""
    code, message = "http_error", "Request failed."
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError, UnicodeDecodeError):
        detail = None
    if isinstance(detail, str):
        message = detail
    elif isinstance(detail, dict):
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
    elif isinstance(detail, list):
        code, message = "invalid_request", "The request was rejected as invalid."
    if status in (401, 403):
        message = f"{message[:_MAX_MESSAGE - len(AUTH_HINT) - 1]} {AUTH_HINT}"
    return MaefClientError(status, code, message[:_MAX_MESSAGE])


class Transport(Protocol):
    def request(self, method: str, path: str, *, json: dict | None = None,
                headers: dict | None = None, data: bytes | None = None) -> tuple[int, dict, bytes]:
        """`data` is a raw text body (used only by draft_write); `json` and `data` are exclusive."""
        ...

    def stream(self, path: str, headers: dict | None = None) -> Iterator[bytes]:
        """Yield response lines (without the trailing newline). Raises MaefClientError on a non-2xx status.
        May end at any time (timeout, server close); the caller reconnects."""
        ...


def _bearer(token: str | None) -> dict:
    token = token if token is not None else os.environ.get("MAEF_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class HttpTransport:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0, *, allow_private: bool = False,
                 policy: EgressPolicy | None = None, token: str | None = None):
        self._auth = _bearer(token)
        self.base_url = (base_url or os.environ.get("MAEF_API_URL") or DEFAULT_URL).rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise MaefClientError(0, "bad_url", "MAEF_API_URL must be an http(s) URL.")
        self.timeout, self.allow_private = timeout, allow_private
        self._loopback = _is_loopback(parsed.hostname)
        self._policy = policy or EgressPolicy()

    def _open(self, req: urllib.request.Request, timeout: float):
        try:
            self._policy.check(req.full_url, allow_loopback=self._loopback, allow_private=self.allow_private)
        except EgressDenied as exc:
            raise MaefClientError(0, "egress_denied", str(exc)) from None
        return opener.open(req, timeout=timeout)  # scheme validated in __init__

    def request(self, method, path, *, json=None, headers=None, data=None):
        hdrs = {**self._auth, **(headers or {})}
        if data is not None:
            hdrs["Content-Type"] = "text/plain; charset=utf-8"
        elif json is not None:
            data = _dumps(json)
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=hdrs)
        try:
            with self._open(req, self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read()
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise MaefClientError(0, "connection_error", "Cannot reach the MAEF API.") from None

    def stream(self, path, headers=None):
        hdrs = {"Accept": "text/event-stream", **self._auth, **(headers or {})}
        req = urllib.request.Request(self.base_url + path, method="GET", headers=hdrs)
        try:
            resp = self._open(req, min(self.timeout, 5.0))  # short reads keep wait() deadlines honest
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            raise error_from_response(exc.code, body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise MaefClientError(0, "connection_error", "Cannot reach the MAEF API.") from None
        with resp:
            try:
                for line in resp:
                    yield line.rstrip(b"\r\n")
            except (TimeoutError, ConnectionError, OSError):
                return  # a quiet or dropped stream is not an error; the caller resumes from its cursor


def _dumps(value) -> bytes:
    return json.dumps(value).encode()


class AsgiTransport:
    """In-process transport over fastapi.testclient (imported lazily: tests only)."""

    def __init__(self, app, token: str | None = None):
        from fastapi.testclient import TestClient
        self._client = TestClient(app)
        self._auth = _bearer(token)

    def request(self, method, path, *, json=None, headers=None, data=None):
        headers = {**self._auth, **(headers or {})}
        if data is not None:
            headers = {"Content-Type": "text/plain; charset=utf-8", **headers}
        resp = self._client.request(method, path, json=json, headers=headers, content=data)
        return resp.status_code, dict(resp.headers), resp.content

    def stream(self, path, headers=None):
        with self._client.stream("GET", path, headers={**self._auth, **(headers or {})}) as resp:
            if resp.status_code >= 300:
                resp.read()
                raise error_from_response(resp.status_code, resp.content)
            for line in resp.iter_lines():
                yield line.encode() if isinstance(line, str) else line


class MaefClient:
    def __init__(self, transport: Transport):
        self._t = transport

    # -- plain JSON calls --
    def _call(self, method: str, path: str, body: dict | None = None):
        status, _, data = self._t.request(method, path, json=body)
        if status >= 300:
            raise error_from_response(status, data)
        try:
            return json.loads(data) if data else None
        except ValueError:
            raise MaefClientError(status, "bad_response", "The API returned a non-JSON response.") from None

    @staticmethod
    def _run_body(agent_id: str, instruction: str, budget: dict | None) -> dict:
        body = {"agent_id": agent_id, "instruction": instruction}
        if budget:
            body["budget"] = budget
        return body

    def list_agents(self) -> list[dict]:
        return self._call("GET", "/api/v1/agents")

    def preview(self, agent_id: str, instruction: str, budget: dict | None = None) -> dict:
        return self._call("POST", "/api/v1/runs/preview", self._run_body(agent_id, instruction, budget))

    def submit(self, agent_id: str, instruction: str, budget: dict | None = None,
               agent_revision: str | None = None) -> dict:
        """-> {"run_id", "status"}. `agent_revision` (commit sha, branch or tag) runs the committed definition."""
        body = self._run_body(agent_id, instruction, budget)
        if agent_revision:
            body["agent_revision"] = agent_revision
        return self._call("POST", "/api/v1/runs", body)

    def schema(self, name: str) -> dict:
        return self._call("GET", f"/api/v1/schemas/{quote(name, safe='')}")

    # -- drafts (server needs MAEF_AUTHORING_ENABLED=1) --
    def draft_create(self, agent_id: str | None = None, new_agent_id: str | None = None) -> dict:
        body = {k: v for k, v in (("agent_id", agent_id), ("new_agent_id", new_agent_id)) if v}
        return self._call("POST", "/api/v1/drafts", body)

    def draft_list(self) -> list[dict]:
        return self._call("GET", "/api/v1/drafts")

    def draft_write(self, draft_id: str, path: str, content: str) -> dict:
        """Replace one file (repo-relative, under agents/<agent>/) with `content`."""
        url = f"/api/v1/drafts/{quote(draft_id, safe='')}/files/{quote(path, safe='/')}"
        status, _, data = self._t.request("PUT", url, data=content.encode("utf-8"))
        if status >= 300:
            raise error_from_response(status, data)
        return json.loads(data)

    def draft_review(self, draft_id: str) -> dict:
        return self._call("GET", f"/api/v1/drafts/{quote(draft_id, safe='')}/review")

    def draft_publish(self, draft_id: str, message: str) -> dict:
        """-> {"draft_id", "revision"}: the commit sha, runnable with `submit(..., agent_revision=sha)`."""
        return self._call("POST", f"/api/v1/drafts/{quote(draft_id, safe='')}/publish", {"message": message})

    def draft_discard(self, draft_id: str) -> dict:
        return self._call("DELETE", f"/api/v1/drafts/{quote(draft_id, safe='')}")

    def decide(self, profile_id: str, state, questions: dict, max_tokens: int | None = None) -> dict:
        """Queue a decision run -> {"run_id", "status"}. `questions` maps name -> {type, instructions, criteria?}."""
        body = {"profile_id": profile_id, "state": state, "questions": questions}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return self._call("POST", "/api/v1/decisions", body)

    def answers(self, run_id: str) -> dict | None:
        """The finished decision's `{"answers": {name: {type, value, ...}}}`, or None when there is none."""
        for ref in self.artifacts(run_id):
            if ref.get("name") == ANSWERS_NAME:
                try:
                    return json.loads(self.download(ref["digest"]))
                except ValueError:
                    raise MaefClientError(0, "bad_response", "The answers artifact is not valid JSON.") from None
        return None

    def get_run(self, run_id: str) -> dict:
        return self._call("GET", f"/api/v1/runs/{quote(run_id, safe='')}")

    def list_runs(self) -> list[dict]:
        return self._call("GET", "/api/v1/runs")

    def cancel(self, run_id: str) -> dict:
        return self._call("POST", f"/api/v1/runs/{quote(run_id, safe='')}/cancel")

    def artifacts(self, run_id: str) -> list[dict]:
        return self._call("GET", f"/api/v1/runs/{quote(run_id, safe='')}/artifacts")

    def download(self, digest: str) -> bytes:
        status, _, data = self._t.request("GET", f"/api/v1/artifacts/{quote(digest, safe='')}")
        if status >= 300:
            raise error_from_response(status, data)
        return data

    # -- events --
    def events(self, run_id: str, after: int = 0, *, deadline: float | None = None) -> Iterator[dict]:
        """Yield event dicts ({"seq","type","ts","payload"}) in order until the terminal event. Reconnects with
        Last-Event-ID from the last seen seq. Stops early (without error) once `deadline` (time.monotonic) passes."""
        cursor, failures = after, 0
        path = f"/api/v1/runs/{quote(run_id, safe='')}/events"
        while True:
            headers = {"Last-Event-ID": str(cursor)} if cursor else None
            progressed = False
            try:
                for frame in _frames(self._t.stream(path, headers)):
                    if frame is not None:
                        seq = frame.get("seq")
                        if isinstance(seq, int) and seq <= cursor:
                            continue
                        if isinstance(seq, int):
                            cursor = seq
                        progressed = True
                        yield frame
                        if frame.get("type") in TERMINAL_EVENTS:
                            return
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                failures = 0
            except MaefClientError as exc:
                if exc.status != 0:
                    raise
                failures += 1
                if failures > _MAX_RECONNECTS:
                    raise
                time.sleep(min(failures, 3) * 0.5)
            if deadline is not None and time.monotonic() >= deadline:
                return
            if not progressed and self.get_run(run_id)["status"] in TERMINAL_STATUSES and failures == 0:
                return  # stream closed on an already-finished run with nothing newer than the cursor
            if not progressed:
                time.sleep(0.2)

    def wait(self, run_id: str, timeout_seconds: float, on_event: Callable[[dict], None] | None = None) -> dict:
        """Follow events until terminal or timeout, then return the run as `get_run` sees it."""
        deadline = time.monotonic() + max(timeout_seconds, 0)
        if timeout_seconds > 0:
            for event in self.events(run_id, deadline=deadline):
                if on_event:
                    on_event(event)
                if time.monotonic() >= deadline:
                    break
        return self.get_run(run_id)

    def result_text(self, run_id: str) -> str | None:
        for ref in self.artifacts(run_id):
            if ref.get("name") == RESULT_NAME:
                return self.download(ref["digest"]).decode("utf-8", errors="replace")
        return None


def _frames(lines: Iterator[bytes]) -> Iterator[dict | None]:
    """Parse SSE lines into event dicts. Yields None for keepalive comments (so callers can check deadlines)."""
    fields: dict[str, list[str]] = {}
    for raw in lines:
        line = raw.decode("utf-8", errors="replace")
        if line == "":
            if "data" in fields:
                event = _decode("\n".join(fields["data"]), fields)
                if event is not None:
                    yield event
            fields = {}
        elif line.startswith(":"):
            yield None
        else:
            name, _, value = line.partition(":")
            fields.setdefault(name, []).append(value.removeprefix(" "))


def _decode(data: str, fields: dict[str, list[str]]) -> dict | None:
    try:
        event = json.loads(data)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    if "seq" not in event and fields.get("id", [""])[-1].isdigit():
        event["seq"] = int(fields["id"][-1])
    if "type" not in event and "event" in fields:
        event["type"] = fields["event"][-1]
    return event
