"""Prompt-cache token accounting: Bedrock usage normalisation, token_count, and the cache fields on
usage.recorded events, OTel span attributes, /api/v1/metrics counters and decision runs. Runs use
tmp_path ledgers and stubbed clients only."""

import hashlib

import boto3
import pytest
import yaml
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from maef_api import main, otel
from maef_api.adapters.base import Message, ModelResponse, ToolCall, token_count
from maef_api.adapters.bedrock import BedrockAdapter
from maef_api.adapters.decision import DecisionRequest, FakeDecisionAdapter
from maef_api.channel import LocalChannel
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.leases import DecisionTask
from maef_api.ledger import Ledger
from maef_api.models import Budget, ExecutionEvent, ModelEndpointProfile
from maef_api.tools import BuiltinTools
from maef_api.worker import Worker
from test_decisions import GOOD, JEV_YAML, QUESTIONS, STATE
from test_run_execution import definition, profile

# Turn 1 calls a tool; its input (100) includes 60 cache reads and 40 cache writes.
TURN_1 = ModelResponse(
    "", (ToolCall("c1", "current_time", {}),), "tool_use", 100, 10, cache_read_tokens=60, cache_write_tokens=40
)
TURN_2 = ModelResponse("Done.", (), "end_turn", 200, 30, cache_read_tokens=150, cache_write_tokens=0)


class Scripted:
    def __init__(self, responses):
        self._responses = iter(responses)

    def converse(self, **kwargs):
        return next(self._responses)


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / "cache.db")
    s.ensure_account("default", 5_000_000)
    return s


def run_cached(store, responses, instruction="Question?"):
    d = definition()
    budget = Budget(max_tokens=200_000, max_turns=5)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=instruction)
    tools = BuiltinTools(["current_time"], {})
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, instruction, tools)
    execute(LedgerSink(store, run_id), run, Scripted(responses))
    return store.get(run_id)


def usage_payloads(run):
    return [e.payload for e in run.events if e.type == "usage.recorded"]


# --- 1. Bedrock Converse usage normalisation (botocore Stubber) ---


def bedrock_converse(usage):
    client = boto3.client("bedrock-runtime", region_name="eu-north-1", aws_access_key_id="x", aws_secret_access_key="y")
    adapter = BedrockAdapter(profile(), client=client)
    reply = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": usage,
        "metrics": {"latencyMs": 1},
    }
    with Stubber(client) as stub:
        stub.add_response("converse", reply)
        return adapter.converse(
            system="s", messages=[Message("user", "q")], tools=[], max_output_tokens=64, timeout_seconds=30
        )


def test_bedrock_cache_tokens_are_added_to_input_tokens():
    out = bedrock_converse(
        {
            "inputTokens": 100,
            "outputTokens": 5,
            "totalTokens": 105,
            "cacheReadInputTokens": 300,
            "cacheWriteInputTokens": 50,
        }
    )
    assert (out.input_tokens, out.cache_read_tokens, out.cache_write_tokens, out.output_tokens) == (450, 300, 50, 5)


def test_bedrock_missing_cache_fields_mean_zero_and_input_unchanged():
    out = bedrock_converse({"inputTokens": 100, "outputTokens": 5, "totalTokens": 105})
    assert (out.input_tokens, out.cache_read_tokens, out.cache_write_tokens, out.output_tokens) == (100, 0, 0, 5)


# --- 2. token_count ---


@pytest.mark.parametrize("value", [None, True, "5", 5.0, -3])
def test_token_count_maps_non_counts_to_zero(value):
    assert token_count(value) == 0


def test_token_count_keeps_positive_ints():
    assert token_count(7) == 7


# --- 3. harness: usage.recorded payloads and tokens charged ---


def test_usage_events_carry_cache_tokens_and_cached_tokens_are_still_charged(store):
    run = run_cached(store, [TURN_1, TURN_2])
    assert run.status == "succeeded"
    assert usage_payloads(run) == [
        {
            "turn": 1,
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "cache_read_tokens": 60,
            "cache_write_tokens": 40,
            "reasoning_tokens": 0,
        },
        {
            "turn": 2,
            "input_tokens": 200,
            "output_tokens": 30,
            "total_tokens": 230,
            "cache_read_tokens": 150,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
    ]
    # Cache reads and writes are already inside input_tokens, so nothing is subtracted from the charge.
    assert run.tokens_used == 110 + 230 == 340
    assert run.tokens_used == sum(p["input_tokens"] + p["output_tokens"] for p in usage_payloads(run))


# --- 4. OTel span attributes ---


def test_chat_spans_and_root_span_carry_cache_attributes(store):
    run = run_cached(store, [TURN_1, TURN_2])
    spans = otel.run_to_spans(run)
    root = next(s for s in spans if s.name == "maef.run")
    chats = sorted((s for s in spans if s.name.startswith("chat ")), key=lambda s: s.attributes["maef.turn"])
    assert len(chats) == 2
    assert [
        (c.attributes["gen_ai.usage.cache_read.input_tokens"], c.attributes["gen_ai.usage.cache_creation.input_tokens"])
        for c in chats
    ] == [(60, 40), (150, 0)]
    assert root.attributes["gen_ai.usage.cache_read.input_tokens"] == 210
    assert root.attributes["gen_ai.usage.cache_creation.input_tokens"] == 40


def test_old_usage_events_without_cache_fields_build_spans_without_cache_attributes(store):
    run = run_cached(store, [TURN_1, TURN_2])
    stripped = [
        ExecutionEvent(
            **{**e.model_dump(), "payload": {k: v for k, v in e.payload.items() if not k.startswith("cache_")}}
        )
        for e in run.events
    ]
    old = run.model_copy(update={"events": stripped})
    assert not any("cache" in k for e in old.events for k in e.payload)  # sanity: really old-style

    spans = otel.run_to_spans(old)  # must not raise
    root = next(s for s in spans if s.name == "maef.run")
    chats = [s for s in spans if s.name.startswith("chat ")]
    assert len(chats) == 2
    for span in [root, *chats]:
        assert not any("cache" in k for k in span.attributes)
    assert root.attributes["gen_ai.usage.input_tokens"] == 300
    assert chats[0].attributes["gen_ai.usage.input_tokens"] == 100


# --- 5. /api/v1/metrics counters ---


@pytest.fixture
def metrics_store(tmp_path, monkeypatch):
    s = Ledger(tmp_path / "metrics.db")
    s.ensure_account("default", 5_000_000)
    monkeypatch.setattr(main, "ledger", s)
    return s


def metric_value(text, sample):
    for line in text.splitlines():
        if line.startswith(sample + " "):
            return float(line.split()[-1])
    raise AssertionError(f"{sample} not in metrics output")


def test_metrics_report_cache_reads_and_writes(metrics_store):
    run_cached(metrics_store, [TURN_1, TURN_2])
    res = TestClient(main.app).get("/api/v1/metrics")
    assert res.status_code == 200
    assert metric_value(res.text, 'maef_cached_input_tokens_total{kind="read"}') == 210
    assert metric_value(res.text, 'maef_cached_input_tokens_total{kind="write"}') == 40


def test_metrics_cache_counters_are_zero_for_old_usage_events(metrics_store):
    run_id = metrics_store.admit(
        definition(), Budget(max_tokens=200_000, max_turns=5), prompt_bytes=10, instruction="old"
    )
    metrics_store.append(
        run_id, "usage.recorded", {"turn": 1, "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    )
    res = TestClient(main.app).get("/api/v1/metrics")
    assert res.status_code == 200
    assert metric_value(res.text, 'maef_tokens_used_total{kind="input"}') == 100  # the event was counted
    assert metric_value(res.text, 'maef_cached_input_tokens_total{kind="read"}') == 0
    assert metric_value(res.text, 'maef_cached_input_tokens_total{kind="write"}') == 0


# --- 6. decision runs record zero cache tokens ---


def test_decision_usage_event_has_zero_cache_fields(tmp_path):
    s = Ledger(tmp_path / "decisions.db")
    s.ensure_account("default", 1_000_000)
    profile_bytes = JEV_YAML.encode()
    task = DecisionTask(
        request=DecisionRequest(state=STATE, questions=QUESTIONS),
        profile=ModelEndpointProfile.model_validate(yaml.safe_load(JEV_YAML)),
        profile_bytes=profile_bytes,
        profile_digest=hashlib.sha256(profile_bytes).hexdigest(),
    )
    run_id = s.admit_decision(task, reserve=50_000, max_duration_seconds=600)
    fake = FakeDecisionAdapter()
    fake.answers = GOOD
    Worker(LocalChannel(s), lambda p: None, worker_id="w1", decision_factory=lambda p: fake).run_once()

    run = s.get(run_id)
    assert run.status == "succeeded"
    usage = usage_payloads(run)
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 100 and usage[0]["output_tokens"] == 10
    assert usage[0]["cache_read_tokens"] == 0 and usage[0]["cache_write_tokens"] == 0
