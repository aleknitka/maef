import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from fastapi.testclient import TestClient

from maef_api import main, otel
from maef_api.adapters import http as http_mod
from maef_api.adapters.base import ModelResponse, ToolCall, ToolSpec
from maef_api.adapters.decision import DecisionRequest, FakeDecisionAdapter
from maef_api.adapters.http import ModelHTTPError
from maef_api.auth import hash_token
from maef_api.channel import LocalChannel
from maef_api.cli import main as cli_main
from maef_api.client import MaefClient
from maef_api.credentials import Credentials
from maef_api.egress import EgressPolicy
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.leases import DecisionTask
from maef_api.ledger import Ledger
from maef_api.models import Budget, ModelEndpointProfile
from maef_api.tools import ToolResult
from maef_api.worker import Worker
from test_decisions import GOOD, JEV_YAML, QUESTIONS, STATE
from test_run_execution import definition

INSTRUCTION = "INSTRUCTION-MARKER-7731"
ARG_MARK = "ARG-MARKER-5512"
RESULT_MARK = "RESULT-MARKER-9034"
FINAL_MARK = "FINAL-TEXT-MARKER-2280"


class LookupTool:
    def specs(self):
        return [ToolSpec("lookup", "Look a key up.", {"type": "object", "properties": {"key": {"type": "string"}}})]

    def invoke(self, name, arguments):
        return ToolResult(RESULT_MARK, False)


class Scripted:
    def __init__(self, responses):
        self._responses = iter(responses)

    def converse(self, **kwargs):
        return next(self._responses)


@pytest.fixture
def store(ledger_target):
    s = Ledger(ledger_target("obs.db"))
    s.ensure_account("default", 5_000_000)
    return s


def run_with(store, responses, *, tools=None, instruction="Question?", budget=None):
    d = definition()
    budget = budget or Budget(max_tokens=200_000, max_turns=5)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=instruction)
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, instruction, tools)
    execute(LedgerSink(store, run_id), run, Scripted(responses))
    return store.get(run_id)


def two_turn_run(store, instruction=INSTRUCTION):
    """Turn 1: one tool call. Turn 2: the final answer. Two model turns, one tool call."""
    return run_with(
        store,
        [
            ModelResponse("", (ToolCall("c1", "lookup", {"key": ARG_MARK}),), "tool_use", 100, 10),
            ModelResponse(FINAL_MARK, (), "end_turn", 200, 30),
        ],
        tools=LookupTool(),
        instruction=instruction,
    )


def decision_run(store):
    profile_bytes = JEV_YAML.encode()
    profile = ModelEndpointProfile.model_validate(yaml.safe_load(JEV_YAML))
    task = DecisionTask(
        request=DecisionRequest(state=STATE, questions=QUESTIONS),
        profile=profile,
        profile_bytes=profile_bytes,
        profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
    )
    run_id = store.admit_decision(task, reserve=50_000, max_duration_seconds=600)
    fake = FakeDecisionAdapter()
    fake.answers = GOOD
    Worker(LocalChannel(store), lambda p: None, worker_id="w1", decision_factory=lambda p: fake).run_once()
    return store.get(run_id)


# --- mapping ---


def test_two_turn_run_maps_to_root_turn_and_tool_spans(store):
    run = two_turn_run(store)
    assert run.status == "succeeded"
    spans = otel.run_to_spans(run)
    roots = [s for s in spans if s.name == "maef.run"]
    turns = sorted((s for s in spans if s.name.startswith("chat ")), key=lambda s: s.attributes["maef.turn"])
    tools = [s for s in spans if s.name == "execute_tool lookup"]
    assert len(roots) == 1 and len(turns) == 2 and len(tools) == 1
    root = roots[0]

    usage = {e.payload["turn"]: e.payload for e in run.events if e.type == "usage.recorded"}
    for turn in turns:
        n = turn.attributes["maef.turn"]
        assert turn.attributes["gen_ai.usage.input_tokens"] == usage[n]["input_tokens"]
        assert turn.attributes["gen_ai.usage.output_tokens"] == usage[n]["output_tokens"]
        assert turn.parent_id == root.span_id
    assert root.attributes["gen_ai.usage.input_tokens"] == 300
    assert root.attributes["gen_ai.usage.output_tokens"] == 40

    tool = tools[0]
    assert tool.attributes["gen_ai.tool.name"] == "lookup"
    assert tool.attributes["maef.tool.is_error"] is False
    assert tool.parent_id == turns[0].span_id  # a tool span hangs off the turn that asked for it

    assert all(s.trace_id == root.trace_id for s in spans)
    assert root.start_ns <= min(s.start_ns for s in spans)
    assert root.end_ns >= max(s.end_ns for s in spans)


def test_ids_are_deterministic_and_differ_between_runs(store):
    run = two_turn_run(store)
    first = otel.run_to_spans(run)
    again = otel.run_to_spans(store.get(run.id))

    def ids(spans):
        return [(s.trace_id, s.span_id, s.parent_id) for s in spans]

    assert ids(first) == ids(again)
    other = otel.run_to_spans(two_turn_run(store))
    assert other[0].trace_id != first[0].trace_id


def test_status_mapping(store):
    ok = otel.run_to_spans(two_turn_run(store))[0]
    assert ok.status == otel.STATUS_OK

    truncated = run_with(store, [ModelResponse("partial", (), "max_tokens", 10, 4096)])
    failed = otel.run_to_spans(truncated)[0]
    assert (failed.status, failed.status_message) == (otel.STATUS_ERROR, "output_truncated")

    queued = store.admit(definition(), Budget(max_tokens=200_000, max_turns=5), prompt_bytes=10, instruction="x")
    store.request_cancel(queued)
    cancelled = otel.run_to_spans(store.get(queued))[0]
    assert cancelled.status == otel.STATUS_UNSET


def test_decision_run_is_a_root_span_only(store):
    run = decision_run(store)
    assert (run.kind, run.status) == ("decision", "succeeded")
    assert [s.name for s in otel.run_to_spans(run)] == ["maef.run"]


def test_no_attribute_carries_instruction_tool_arguments_or_result_text(store):
    spans = otel.run_to_spans(two_turn_run(store))
    text = json.dumps(
        [{"name": s.name, "attributes": s.attributes, "message": s.status_message} for s in spans], default=str
    )
    for marker in (INSTRUCTION, ARG_MARK, RESULT_MARK, FINAL_MARK):
        assert marker not in text


def test_to_otlp_envelope_and_id_lengths(store):
    spans = otel.run_to_spans(two_turn_run(store))
    body = otel.to_otlp(spans)
    resource = body["resourceSpans"][0]
    attributes = {a["key"]: a["value"] for a in resource["resource"]["attributes"]}
    assert attributes["service.name"] == {"stringValue": "maef"}
    wire = resource["scopeSpans"][0]["spans"]
    assert len(wire) == len(spans)
    assert sum("parentSpanId" not in s for s in wire) == 1
    for s in wire:
        assert len(s["traceId"]) == 32 and len(s["spanId"]) == 16
        if "parentSpanId" in s:
            assert len(s["parentSpanId"]) == 16


# --- OTLP/HTTP export against a local stub collector ---


class Collector:
    def __init__(self, statuses=(200,)):
        self.statuses, self.requests = list(statuses), []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                collector.requests.append((self.path, self.headers, body))
                status = collector.statuses.pop(0) if len(collector.statuses) > 1 else collector.statuses[0]
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    stub = Collector()
    yield stub
    stub.close()


def test_exporter_posts_json_with_credential_headers(collector, store):
    spans = otel.run_to_spans(two_turn_run(store))
    exporter = otel.OtlpHttpExporter(
        collector.url, "env:OTLP_HEADERS", credentials=Credentials({"OTLP_HEADERS": "x-api-key=k-123, x-tenant=t9"})
    )
    exporter.export(spans)
    path, headers, body = collector.requests[0]
    assert path == "/v1/traces"
    assert headers.get("Content-Type") == "application/json"
    assert headers.get("x-api-key") == "k-123" and headers.get("x-tenant") == "t9"
    assert json.loads(body) == otel.to_otlp(spans)


def test_server_error_is_retried_then_raised(collector, store):
    collector.statuses = [503]
    exporter = otel.OtlpHttpExporter(collector.url, max_retries=1)
    with pytest.raises(otel.OtlpExportError):
        exporter.export(otel.run_to_spans(two_turn_run(store)))
    assert len(collector.requests) == 2


def test_metadata_address_is_refused_without_a_request(store, monkeypatch):
    sent = []

    class NoSend:
        def open(self, req, timeout=None):
            sent.append(req)
            raise AssertionError("no request should be sent")

    monkeypatch.setattr(http_mod, "opener", NoSend())

    def resolver(host, port, **_):
        return [(2, 1, 6, "", ("169.254.169.254", port))]

    exporter = otel.OtlpHttpExporter("http://otel.example:4318", policy=EgressPolicy(resolver=resolver))
    with pytest.raises(otel.OtlpExportError, match="egress_denied:forbidden_address"):
        exporter.export(otel.run_to_spans(two_turn_run(store)))
    assert sent == []


def test_export_pending_exports_terminal_runs_once_and_marks_them(store):
    two_turn_run(store)
    run_with(store, [ModelResponse("ok", (), "end_turn", 5, 1)])
    store.admit(definition(), Budget(max_tokens=200_000, max_turns=5), prompt_bytes=10, instruction="still queued")
    sent = []
    exporter = otel.OtlpHttpExporter("http://127.0.0.1:9", post=lambda url, headers, body, **kw: sent.append(body))

    assert otel.export_pending(store, exporter) == 2
    assert len(sent) == 2
    assert store.unexported_runs() == []
    assert otel.export_pending(store, exporter) == 0
    assert len(sent) == 2


def test_failing_exporter_leaves_runs_unmarked(store):
    two_turn_run(store)
    run_with(store, [ModelResponse("ok", (), "end_turn", 5, 1)])

    def down(url, headers, body, **kw):
        raise ModelHTTPError(503, "http_error")

    exporter = otel.OtlpHttpExporter("http://127.0.0.1:9", post=down)
    assert otel.export_pending(store, exporter) == 0
    assert len(store.unexported_runs()) == 2


def test_cli_otel_export_sends_finished_runs(live_api, collector, capsys):
    client = MaefClient(live_api.transport)
    run_id = client.submit("research-analyst", "Hello")["run_id"]
    live_api.finish_queued()
    code = cli_main(["otel", "export", "--endpoint", collector.url, "--json"], client=client)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"exported": 1}
    path, _, body = collector.requests[0]
    assert path == "/v1/traces"
    wire = json.loads(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    roots = [s for s in wire if "parentSpanId" not in s]
    assert len(roots) == 1 and roots[0]["traceId"] == otel.trace_id_for(run_id)
    assert any(s["name"].startswith("chat ") for s in wire)


# --- /api/v1/metrics ---

METRIC_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[^}]*\})?) (\S+)$")


def parse_metrics(text):
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = METRIC_LINE.match(line)
        assert match, f"not a sample line: {line!r}"
        samples[match.group(1)] = float(match.group(2))
    return samples


@pytest.fixture
def metrics_store(store, monkeypatch):
    monkeypatch.setattr(main, "ledger", store)
    store.register_api_token("test-admin", ("admin",), hash_token(os.environ["MAEF_API_TOKEN"]))
    return store


def test_metrics_requires_read_scope(metrics_store):
    anonymous = TestClient(main.app)
    del anonymous.headers["Authorization"]
    assert anonymous.get("/api/v1/metrics").status_code == 401

    metrics_store.register_api_token("runs-only", ("runs:write",), hash_token("maef_runs-only-token"))
    limited = TestClient(main.app, headers={"Authorization": "Bearer maef_runs-only-token"})
    assert limited.get("/api/v1/metrics").status_code == 403


def test_metrics_counters_match_known_runs(metrics_store):
    store = metrics_store
    two_turn_run(store)  # succeeded: 2 turns, 300 in / 40 out, one tool call (ok), artifact FINAL_MARK
    # Reservation (50 tokens) is smaller than one turn's worst case: stopped before any model call, 0 tokens.
    run_with(store, [], budget=Budget(max_tokens=50, max_turns=5))
    store.request_cancel(
        store.admit(definition(), Budget(max_tokens=200_000, max_turns=5), prompt_bytes=10, instruction="cancel me")
    )
    store.admit(definition(), Budget(max_tokens=200_000, max_turns=5), prompt_bytes=10, instruction="lost")
    store.claim("w1", 60, now=1000)
    store.expire_leases(now=1061)

    res = TestClient(main.app).get("/api/v1/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    samples = parse_metrics(res.text)
    expected = {
        'maef_runs_total{status="admitted",kind="agent"}': 0,
        'maef_runs_total{status="leased",kind="agent"}': 0,
        'maef_runs_total{status="running",kind="agent"}': 0,
        'maef_runs_total{status="succeeded",kind="agent"}': 1,
        'maef_runs_total{status="failed",kind="agent"}': 2,
        'maef_runs_total{status="cancelled",kind="agent"}': 1,
        'maef_tokens_used_total{kind="input"}': 300,
        'maef_tokens_used_total{kind="output"}': 40,
        "maef_tokens_reserved_open": 0,
        "maef_run_duration_seconds_count": 4,
        'maef_budget_exhaustions_total{reason="tokens"}': 1,
        'maef_budget_exhaustions_total{reason="turns"}': 0,
        'maef_budget_exhaustions_total{reason="duration"}': 0,
        "maef_worker_lost_total": 1,
        'maef_tool_calls_total{is_error="false"}': 1,
        'maef_tool_calls_total{is_error="true"}': 0,
        "maef_artifact_bytes_total": len(FINAL_MARK.encode()),
    }
    assert {k: samples[k] for k in expected} == expected
    assert sum(k.startswith("maef_runs_total{") for k in samples) == 6
