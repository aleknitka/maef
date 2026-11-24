"""OpenTelemetry traces from the run ledger. Stdlib only; OTLP/HTTP JSON.

Interface: `run_to_spans(run)` is a pure mapping from a RunDetail to spans; `OtlpHttpExporter.export(spans)` posts
them; `export_pending(ledger, exporter)` exports every terminal run once (the background exporter's tick).

Trace and span ids are sha256 truncations of the run id and event seq, so re-exporting a run yields the same ids
and a collector can deduplicate. Events carry sizes and identifiers, never prompt, tool argument or result
content, and spans only copy those fields; keep it that way when adding attributes. Attribute names follow the
OTel GenAI semantic conventions (gen_ai.*) where they fit and `maef.*` elsewhere; the conventions are still
evolving, so the version this was written against is recorded as `maef.semconv`.
"""
import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from .adapters.http import ModelHTTPError, post_json
from .credentials import CredentialError, Credentials
from .egress import EgressPolicy
from .ledger import TERMINAL, Ledger
from .models import RunDetail, is_loopback_host

SEMCONV = "gen_ai-1.37"
SCOPE_NAME = "maef.ledger"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_log = logging.getLogger("maef.otel")

KIND_INTERNAL, KIND_CLIENT = 1, 3
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2


@dataclass(frozen=True)
class Span:
    trace_id: str  # 32 hex
    span_id: str  # 16 hex
    parent_id: str | None
    name: str
    start_ns: int
    end_ns: int
    kind: int = KIND_INTERNAL
    attributes: dict = field(default_factory=dict)
    status: int = STATUS_UNSET
    status_message: str = ""


def _digest(text: str, length: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def trace_id_for(run_id: str) -> str:
    return _digest(f"maef:trace:{run_id}", 32)


def span_id_for(run_id: str, key: str) -> str:
    return _digest(f"maef:span:{run_id}:{key}", 16)


def _ns(ts: str) -> int:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return ((dt - _EPOCH) // timedelta(microseconds=1)) * 1000


def _attrs(**values) -> dict:
    return {k: v for k, v in values.items() if v is not None}


def run_to_spans(run: RunDetail) -> list[Span]:
    """Root span `maef.run`, plus a child per model turn, tool call, sandbox and MCP server lifecycle."""
    events = run.events
    if not events:
        return []
    trace = trace_id_for(run.id)
    root_id = span_id_for(run.id, "root")
    last_ns = _ns(events[-1].ts)
    created = next((e for e in events if e.type == "task.created"), events[0])
    terminal = next((e for e in reversed(events) if e.type in tuple(f"task.{s}" for s in TERMINAL)), None)
    start_ns = _ns(created.ts)
    end_ns = _ns(terminal.ts) if terminal else last_ns
    profile = next((e.payload.get("profile") for e in events if e.type == "model.request"), None)
    status, message = {"succeeded": (STATUS_OK, ""), "failed": (STATUS_ERROR, run.terminal_reason or "failed")}.get(
        run.status, (STATUS_UNSET, ""))
    spans = [Span(
        trace, root_id, None, "maef.run", start_ns, max(end_ns, start_ns), KIND_INTERNAL,
        _attrs(**{
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": run.agent_id,
            "gen_ai.request.model": profile,
            "gen_ai.usage.input_tokens": _sum(events, "usage.recorded", "input_tokens"),
            "gen_ai.usage.output_tokens": _sum(events, "usage.recorded", "output_tokens"),
            "gen_ai.usage.cache_read.input_tokens": _sum(events, "usage.recorded", "cache_read_tokens"),
            "gen_ai.usage.cache_creation.input_tokens": _sum(events, "usage.recorded", "cache_write_tokens"),
            "maef.usage.reasoning_tokens": _sum(events, "usage.recorded", "reasoning_tokens"),
            "maef.usage.cost_usd": _sum(events, "usage.recorded", "cost_usd", (int, float)),
            "maef.run_id": run.id, "maef.run.kind": run.kind, "maef.run.status": run.status,
            "maef.agent_revision": run.git_rev, "maef.dirty": run.dirty,
            "maef.terminal_reason": run.terminal_reason, "maef.tokens_used": run.tokens_used,
            "maef.tokens_reserved": run.reserved_tokens, "maef.semconv": SEMCONV}),
        status, message)]

    def child(event, name, end_ts_ns, *, kind=KIND_INTERNAL, parent=root_id, attributes=None, status=STATUS_UNSET,
              message="") -> Span:
        start = _ns(event.ts)
        return Span(trace, span_id_for(run.id, str(event.seq)), parent, name, start, max(end_ts_ns, start), kind,
                    _attrs(**(attributes or {})), status, message)

    turns: dict[int, Span] = {}
    pending_requests: dict[int, tuple] = {}  # turn -> (request event, response payload, usage payload, end_ns)
    for e in events:
        turn = e.payload.get("turn")
        if e.type == "model.request" and isinstance(turn, int):
            pending_requests[turn] = (e, {}, {}, _ns(e.ts))
        elif e.type in ("model.response", "usage.recorded") and turn in pending_requests:
            req, resp, usage, _ = pending_requests[turn]
            pending_requests[turn] = (req, e.payload if e.type == "model.response" else resp,
                                      e.payload if e.type == "usage.recorded" else usage, _ns(e.ts))
    for turn, (req, resp, usage, end) in pending_requests.items():
        p = req.payload
        span = child(req, f"chat {p.get('profile', 'model')}", end, kind=KIND_CLIENT, attributes={
            "gen_ai.operation.name": "chat", "gen_ai.request.model": p.get("profile"),
            "gen_ai.request.max_tokens": p.get("max_output_tokens"),
            "gen_ai.response.finish_reasons": [resp["stop_reason"]] if "stop_reason" in resp else None,
            "gen_ai.usage.input_tokens": usage.get("input_tokens"),
            "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            "gen_ai.usage.cache_read.input_tokens": usage.get("cache_read_tokens"),
            "gen_ai.usage.cache_creation.input_tokens": usage.get("cache_write_tokens"),
            "maef.usage.reasoning_tokens": usage.get("reasoning_tokens"), "maef.usage.cost_usd": usage.get("cost_usd"),
            "gen_ai.response.id": resp.get("response_id"), "gen_ai.response.model": resp.get("model"),
            "maef.provider": resp.get("provider"), "maef.latency_ms": resp.get("latency_ms"),
            "maef.turn": turn, "maef.model_profile": p.get("profile"), "maef.stop_reason": resp.get("stop_reason"),
            "maef.prompt_bytes": p.get("prompt_bytes"), "maef.tools_offered": p.get("tools"),
            "maef.tool_calls": resp.get("tool_calls")})
        turns[turn] = span
        spans.append(span)

    open_calls: dict[tuple, list] = {}
    for e in events:
        key = (e.payload.get("turn"), e.payload.get("name"))
        if e.type == "tool.call":
            open_calls.setdefault(key, []).append(e)
        elif e.type == "tool.result":
            queue = open_calls.get(key)
            if queue:
                spans.append(_tool_span(child, queue.pop(0), e, turns))
    for queue in open_calls.values():  # a call with no result: cut off by a cancel or crash
        for call in queue:
            spans.append(_tool_span(child, call, None, turns, last_ns))

    sandbox, mcp_start, mcp_prov, closed = None, {}, {}, set()
    for e in events:
        p = e.payload
        if e.type == "sandbox.provisioned":
            if p.get("server"):
                mcp_prov[p["server"]] = e
            else:
                sandbox = e
        elif e.type == "sandbox.torn_down" and sandbox is not None:
            spans.append(child(sandbox, "maef.sandbox", _ns(e.ts), attributes=_sandbox_attrs(sandbox.payload)))
            sandbox = None
        elif e.type == "mcp.started":
            mcp_start[p.get("server")] = e
        elif e.type == "mcp.stopped" and p.get("server") in mcp_start:
            spans.append(_mcp_span(child, p["server"], mcp_prov.get(p["server"]), mcp_start[p["server"]], _ns(e.ts)))
            closed.add(p["server"])
    if sandbox is not None:
        spans.append(child(sandbox, "maef.sandbox", last_ns, attributes=_sandbox_attrs(sandbox.payload)))
    for server, started in mcp_start.items():  # started but never stopped
        if server not in closed:
            spans.append(_mcp_span(child, server, mcp_prov.get(server), started, last_ns))
    return spans


def _sum(events, type_, field_, kind=int) -> int | float | None:
    values = [e.payload[field_] for e in events if e.type == type_ and isinstance(e.payload.get(field_), kind)
              and not isinstance(e.payload.get(field_), bool)]
    return sum(values) if values else None


def _tool_span(child, call, result, turns, fallback_end: int | None = None) -> Span:
    p = call.payload
    r = result.payload if result is not None else {}
    is_error = bool(r.get("is_error"))
    parent = turns[p["turn"]].span_id if p.get("turn") in turns else None
    return child(
        call, f"execute_tool {p.get('name')}", _ns(result.ts) if result is not None else fallback_end,
        **({"parent": parent} if parent else {}),
        attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": p.get("name"),
                    "maef.turn": p.get("turn"), "maef.tool.server": p.get("server"),
                    "maef.tool.is_error": is_error if result is not None else None,
                    "maef.tool.argument_bytes": p.get("argument_bytes"), "maef.tool.result_bytes": r.get("result_bytes"),
                    "maef.tool.duration_ms": r.get("duration_ms")},
        status=STATUS_ERROR if is_error else STATUS_UNSET, message="tool_error" if is_error else "")


def _sandbox_attrs(p: dict) -> dict:
    return {"maef.sandbox.profile": p.get("profile"), "maef.sandbox.network": p.get("network"),
            "maef.sandbox.network_open": p.get("network_open")}


def _mcp_span(child, server, provisioned, started, end_ns) -> Span:
    s = started.payload
    attributes = {"maef.mcp.server": server, "maef.mcp.version": s.get("version"),
                  "maef.mcp.tool_count": s.get("tool_count"), "maef.mcp.network": s.get("network"),
                  "maef.sandbox.network_open": provisioned.payload.get("network_open") if provisioned else None}
    return child(provisioned or started, f"maef.mcp {server}", end_ns, attributes=attributes)


# --- OTLP/HTTP JSON ---

def _value(v) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_value(x) for x in v]}}
    return {"stringValue": str(v)}


def _kv(attributes: dict) -> list[dict]:
    return [{"key": k, "value": _value(v)} for k, v in attributes.items()]


def to_otlp(spans: list[Span]) -> dict:
    """The `ExportTraceServiceRequest` JSON body for these spans."""
    wire = []
    for s in spans:
        item = {"traceId": s.trace_id, "spanId": s.span_id, "name": s.name, "kind": s.kind,
                "startTimeUnixNano": str(s.start_ns), "endTimeUnixNano": str(s.end_ns),
                "attributes": _kv(s.attributes), "status": {"code": s.status}}
        if s.parent_id:
            item["parentSpanId"] = s.parent_id
        if s.status_message:
            item["status"]["message"] = s.status_message
        wire.append(item)
    return {"resourceSpans": [{
        "resource": {"attributes": _kv({"service.name": "maef"})},
        "scopeSpans": [{"scope": {"name": SCOPE_NAME}, "spans": wire}]}]}


class OtlpExportError(Exception):
    """Names the failure kind only; never the endpoint, headers or body."""


class OtlpHttpExporter:
    """POSTs spans to `{endpoint}/v1/traces`. Egress is checked like every outbound call: loopback only when the
    endpoint itself is loopback, private networks only with `allow_private`. `headers_ref` is a credential ref
    (`env:NAME`) whose value is `Name=value,Name2=value2` (the OTEL_EXPORTER_OTLP_HEADERS format)."""

    def __init__(self, endpoint: str, headers_ref: str | None = None, *, credentials: Credentials | None = None,
                 allow_private: bool = False, policy: EgressPolicy | None = None, timeout: float = 10.0,
                 max_retries: int = 2, post: Callable = post_json):
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise OtlpExportError("bad_endpoint")
        self._url = endpoint.rstrip("/") + "/v1/traces"
        self._loopback = is_loopback_host(parsed.hostname)
        self._headers_ref, self._credentials = headers_ref, credentials or Credentials()
        self._allow_private, self._policy = allow_private, policy or EgressPolicy()
        self._timeout, self._retries, self._post = timeout, max_retries, post

    def _headers(self) -> dict:
        if not self._headers_ref:
            return {}
        try:
            secret = self._credentials.resolve(self._headers_ref)
        except CredentialError as exc:
            raise OtlpExportError(str(exc)) from None
        headers = {}
        for pair in (secret.reveal().split(",") if secret else []):
            name, sep, value = pair.partition("=")
            if sep and name.strip():
                headers[name.strip()] = value.strip()
        return headers

    def export(self, spans: list[Span]) -> None:
        if not spans:
            return
        try:
            self._post(self._url, self._headers(), to_otlp(spans), timeout=self._timeout, max_retries=self._retries,
                       policy=self._policy, allow_loopback=self._loopback, allow_private=self._allow_private)
        except ModelHTTPError as exc:
            raise OtlpExportError(str(exc)) from None
        except ValueError:
            return  # accepted with an empty or non-JSON body, which OTLP servers may send


def export_pending(ledger: Ledger, exporter: OtlpHttpExporter, limit: int = 20) -> int:
    """Export up to `limit` terminal runs not exported yet, marking each after success. A failure stops the batch
    (the collector is probably down), is logged by class and message, and leaves the run to be retried."""
    done = 0
    for run_id in ledger.unexported_runs(limit):
        run = ledger.get(run_id)
        if run is None:
            continue
        try:
            exporter.export(run_to_spans(run))
        except OtlpExportError as exc:
            _log.warning("otlp export failed for a run: %s", exc)
            break
        ledger.mark_exported(run_id)
        done += 1
    return done


def exporter_from_env(environ=os.environ) -> OtlpHttpExporter | None:
    """The background exporter configured by MAEF_OTLP_ENDPOINT (+ MAEF_OTLP_HEADERS_REF, MAEF_OTLP_ALLOW_PRIVATE=1)."""
    endpoint = environ.get("MAEF_OTLP_ENDPOINT")
    if not endpoint:
        return None
    return OtlpHttpExporter(endpoint, environ.get("MAEF_OTLP_HEADERS_REF") or None,
                            allow_private=environ.get("MAEF_OTLP_ALLOW_PRIVATE") == "1")
