import json

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from maef_api import main
from maef_api.adapters.base import Message, ModelResponse
from maef_api.adapters.bedrock import BedrockAdapter
from maef_api.channel import LocalChannel
from maef_api.definitions import PinnedDefinition, compute_definition_digest
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.ledger import Ledger
from maef_api.models import AgentDefinition, Budget, ModelEndpointProfile
from maef_api.runs import Runs
from maef_api.worker import Worker
from test_api import PROFILE


def profile(extra=""):
    import yaml

    return ModelEndpointProfile.model_validate(yaml.safe_load(PROFILE.format(id="test-model", extra=extra)))


def definition(dirty=False):
    agent = AgentDefinition(
        schema_version="maef.dev/v1alpha1", id="research-analyst", version="0.1.0", display_name="R", description="d"
    )
    return PinnedDefinition(
        agent=agent,
        profile=profile(),
        instructions="You are an analyst.",
        git_rev="abc",
        dirty=dirty,
        definition_digest="d",
        profile_digest="p",
    )


class FakeAdapter:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def converse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / "l.db")
    s.ensure_account("default", 1_000_000)
    return s


def prepared(store, **kw):
    d = definition()
    budget = Budget(max_tokens=500_000, max_turns=1, **kw)
    run_id = store.admit(d, budget, prompt_bytes=28, instruction="Question?")
    return PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, "Question?")


def types(store, run_id):
    return [e.type for e in store.get(run_id).events]


def test_successful_run(store):
    run = prepared(store)
    adapter = FakeAdapter(ModelResponse("The answer.", (), "end_turn", 1000, 200))
    execute(LedgerSink(store, run.run_id), run, adapter)
    detail = store.get(run.run_id)
    assert detail.status == "succeeded" and detail.tokens_used == 1200
    assert types(store, run.run_id) == [
        "task.created",
        "task.admitted",
        "run.started",
        "model.request",
        "model.response",
        "usage.recorded",
        "artifact.stored",
        "run.output",
        "run.manifest",
        "task.succeeded",
    ]
    assert adapter.calls[0]["max_output_tokens"] == 4096
    output = next(e for e in detail.events if e.type == "run.output").payload
    assert set(output) == {"digest", "size", "mime", "name"} and output["name"] == "output.md"
    manifest = next(e for e in detail.events if e.type == "run.manifest").payload
    assert manifest["artifacts"] == [output]
    assert manifest["agent_revision"] == "abc" and manifest["tokens_used"] == 1200
    acct = store.account("default")
    assert (acct["reserved_tokens"], acct["spent_tokens"]) == (0, 1200)


def test_prompts_not_logged(store):
    run = prepared(store)
    execute(LedgerSink(store, run.run_id), run, FakeAdapter(ModelResponse("ok", (), "end_turn", 10, 1)))
    dump = json.dumps([e.model_dump() for e in store.get(run.run_id).events if e.type != "run.output"])
    assert "Question?" not in dump and "You are an analyst" not in dump


def test_truncated_output_fails_but_is_kept(store):
    run = prepared(store)
    execute(LedgerSink(store, run.run_id), run, FakeAdapter(ModelResponse("partial", (), "max_tokens", 10, 4096)))
    detail = store.get(run.run_id)
    assert detail.status == "failed" and detail.terminal_reason == "output_truncated"
    assert "run.output" in types(store, run.run_id)


def test_adapter_error_records_class_only_and_releases(store):
    run = prepared(store)
    execute(LedgerSink(store, run.run_id), run, FakeAdapter(error=TimeoutError("secret prompt echo")))
    detail = store.get(run.run_id)
    assert detail.terminal_reason == "provider_error" and detail.tokens_used == 0
    assert "secret prompt echo" not in json.dumps([e.model_dump() for e in detail.events])
    assert store.account("default")["reserved_tokens"] == 0


def test_duration_exhausted_skips_model_call(store):
    run = prepared(store)
    ticks = iter([0, 10_000, 10_000])
    adapter = FakeAdapter(ModelResponse("x", (), "end_turn", 1, 1))
    execute(LedgerSink(store, run.run_id), run, adapter, clock=lambda: next(ticks))
    assert adapter.calls == [] and store.get(run.run_id).terminal_reason == "duration_exhausted"


def test_recover_fails_orphans_closed(store):
    run = prepared(store)
    assert store.claim("w1", 60, now=1000) is not None
    assert store.expire_leases(now=1030) == 0  # lease still valid
    assert store.expire_leases(now=1061) == 1
    detail = store.get(run.run_id)
    assert detail.terminal_reason == "worker_lost" and detail.tokens_used == run.reserved_tokens
    assert store.account("default")["spent_tokens"] == run.reserved_tokens


def test_bedrock_request_shape_and_usage_parsing():
    client = boto3.client("bedrock-runtime", region_name="eu-north-1", aws_access_key_id="x", aws_secret_access_key="y")
    adapter = BedrockAdapter(profile(), client=client)
    with Stubber(client) as stub:
        stub.add_response(
            "converse",
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "Hi "}, {"text": "there"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15},
                "metrics": {"latencyMs": 1},
            },
            expected_params={
                "modelId": "global.test.model-1",
                "system": [{"text": "sys"}],
                "messages": [{"role": "user", "content": [{"text": "q"}]}],
                "inferenceConfig": {"maxTokens": 256},
            },
        )
        out = adapter.converse(
            system="sys", messages=[Message("user", "q")], tools=[], max_output_tokens=256, timeout_seconds=30
        )
    assert (out.text, out.stop_reason, out.input_tokens, out.output_tokens) == ("Hi there", "end_turn", 12, 3)


def test_bedrock_rejects_other_dialects_and_credentials():
    with pytest.raises(ValueError):
        BedrockAdapter(profile().model_copy(update={"credential_ref": "env:aws-keys"}))


# --- HTTP ---
@pytest.fixture
def api(tmp_path, monkeypatch):
    s = Ledger(tmp_path / "api.db")
    s.ensure_account("default", 5_000_000)
    monkeypatch.setenv("MAEF_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("MAEF_ALLOW_DIRTY", "1")
    defs = main.runs.definitions
    pinned = definition(dirty=True)
    pinned = pinned.model_copy(
        update={"definition_digest": compute_definition_digest(pinned.definition_bytes, pinned.files)}
    )
    monkeypatch.setattr(defs, "resolve", lambda agent_id: pinned)
    adapter = FakeAdapter(ModelResponse("Done.", (), "end_turn", 100, 10))
    monkeypatch.setattr(
        main, "runs", Runs(defs, s, execution_enabled=main.execution_enabled, allow_dirty=main.allow_dirty)
    )
    return TestClient(main.app), s, Worker(LocalChannel(s), lambda p: adapter, worker_id="w1")


def test_post_runs_queues_and_worker_executes(api):
    client, _, worker = api
    res = client.post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": "Hello"})
    assert res.status_code == 202
    run_id = res.json()["run_id"]
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "admitted"
    assert worker.run_once() is True
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "succeeded"


def test_post_runs_disabled_flag_and_health(api, monkeypatch):
    client, _, _ = api
    assert client.get("/health").json()["execution_enabled"] is True
    monkeypatch.delenv("MAEF_EXECUTION_ENABLED")
    assert client.get("/health").json()["execution_enabled"] is False
    assert client.post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": "x"}).status_code == 501


def test_post_runs_refuses_dirty_without_flag(api, monkeypatch):
    client, s, _ = api
    monkeypatch.delenv("MAEF_ALLOW_DIRTY")
    res = client.post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": "x"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "dirty" and s.list() == []
