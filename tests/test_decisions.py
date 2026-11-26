"""Decision models: request validation, both provider adapters against a fake HTTP server, egress,
the decision run end to end (API -> worker -> answers.json, metering, cancel), and the client, CLI
and gateway surfaces. Wire formats are in docs/DECISION_MODELS.md."""

import copy
import json
import re
import socket

import pytest
import yaml
from pydantic import ValidationError

from maef_api import main
from maef_api.adapters import http as http_mod
from maef_api.adapters.decision import (
    Answer,
    DecisionRequest,
    DecisionsApiAdapter,
    FakeDecisionAdapter,
    Question,
    WorkersAiDecisionAdapter,
)
from maef_api.adapters.http import ModelHTTPError
from maef_api.channel import LocalChannel
from maef_api.client import MaefClient, MaefClientError
from maef_api.credentials import Credentials
from maef_api.egress import EgressPolicy
from maef_api.models import ModelEndpointProfile
from maef_api.registry import ModelProfileRegistry
from maef_api.worker import Worker
from test_cli import cli
from test_gateway import call, text_of, wrapped
from test_http_adapters import Fake
from test_run_execution import definition

KEY = "key-123"
CREDS = Credentials({"DECISION_KEY": KEY})
ACCOUNT = "a" * 32
CHOICE = {"type": "choice", "instructions": "Which lane?", "criteria": {"alpha": "quick", "omega": "thorough"}}
SCORE = {"type": "score", "instructions": "How risky?", "criteria": ["low", "medium", "high"]}
NOUL = {"type": "noul", "instructions": "Is it urgent?"}
QUESTIONS = {"lane": CHOICE, "risk": SCORE, "urgent": NOUL}
STATE = "STATE-MARKER-4242 patient reports chest pain"
GOOD = {
    "lane": {"type": "choice", "choice": "omega", "probabilities": {"alpha": 0.2, "omega": 0.8}, "confidence": 0.8},
    "risk": {
        "type": "score",
        "score": 1.25,
        "legend": {"0": "low", "1": "medium", "2": "high"},
        "probabilities": {"0": 0.0, "1": 0.75, "2": 0.25},
        "confidence": 0.5,
    },
    "urgent": {"type": "noul", "noul": 0.91},
}
# What the run stores in answers.json: the parsed Answer values, without empty fields.
EXPECTED = {
    "lane": {"type": "choice", "value": "omega", "confidence": 0.8, "probabilities": {"alpha": 0.2, "omega": 0.8}},
    "risk": {"type": "score", "value": 1.25, "confidence": 0.5, "probabilities": {"0": 0.0, "1": 0.75, "2": 0.25}},
    "urgent": {"type": "noul", "value": 0.91},
}
CLEF = {"model": "clef-omni", "answers": GOOD, "usage": {"input_tokens": 12, "output_tokens": 3}}

JEV_YAML = """schema_version: maef.dev/v1alpha1
id: jev-decisions
version: 0.1.0
display_name: Jev
vendor: TypeSafe
api_dialect: decisions-api
endpoint: {kind: decisions-api, base_url: "https://decisions-api.dev", model_id: "typesafe/jev-1.13"}
credential_ref: env:DECISION_KEY
"""


def _profile(endpoint: dict, dialect: str) -> ModelEndpointProfile:
    return ModelEndpointProfile.model_validate(
        {
            "schema_version": "maef.dev/v1alpha1",
            "id": "dec-one",
            "version": "0.1.0",
            "display_name": "T",
            "vendor": "T",
            "api_dialect": dialect,
            "endpoint": endpoint,
            "credential_ref": "env:DECISION_KEY",
            "max_retries": 0,
        }
    )


def jev_profile(base_url: str) -> ModelEndpointProfile:
    return _profile({"kind": "decisions-api", "base_url": base_url, "model_id": "typesafe/jev-1.13"}, "decisions-api")


def clef_profile(base_url: str) -> ModelEndpointProfile:
    return _profile(
        {
            "kind": "cloudflare-workers-ai",
            "base_url": base_url + "/client/v4",
            "account_id": ACCOUNT,
            "model_id": "@cf/cloudflare/clef-omni",
        },
        "workers-ai-decision",
    )


def request(state=STATE, questions=QUESTIONS) -> DecisionRequest:
    return DecisionRequest(state=state, questions=questions)


def envelope(answers, usage=None):
    """decisions-api.dev success response: HTTP 200 with the code/data wrapper."""
    result = {
        "model": "typesafe/jev-1.13",
        "answers": answers,
        "usage": {"input_tokens": 30, "output_tokens": 4} if usage is None else usage,
        "elapsedMs": 12,
    }
    return (
        200,
        {
            "code": 0,
            "message": "ok",
            "data": {"requestId": "r1", "creditsUsed": 1, "historySaved": True, "result": result},
        },
    )


@pytest.fixture
def server():
    made = []

    def make(replies):
        made.append(Fake(replies))
        return made[-1]

    yield make
    for f in made:
        f.close()


# --- request validation ---


def test_question_type_and_criteria_bounds():
    with pytest.raises(ValidationError):
        Question(type="essay", instructions="x")
    with pytest.raises(ValidationError):
        Question(type="choice", instructions="x", criteria={"only": "one"})
    with pytest.raises(ValidationError):
        Question(type="choice", instructions="x", criteria={f"o{i}": "d" for i in range(21)})
    assert Question(type="choice", instructions="x", criteria={f"o{i}": "d" for i in range(20)}).type == "choice"
    with pytest.raises(ValidationError):
        Question(type="score", instructions="x", criteria=["only"])
    with pytest.raises(ValidationError):
        Question(type="score", instructions="x", criteria=[str(i) for i in range(11)])
    assert Question(type="score", instructions="x", criteria=[str(i) for i in range(10)]).type == "score"
    with pytest.raises(ValidationError):
        Question(type="noul", instructions="x", criteria={"yes": "y"})
    assert Question(type="noul", instructions="x", criteria={"true": "t", "false": "f"}).type == "noul"


@pytest.mark.parametrize("name", ["Upper", "1digit", "has-dash", "a" * 42])
def test_question_names_must_match_pattern(name):
    with pytest.raises(ValidationError):
        request(questions={name: NOUL})


def test_question_count_is_one_to_eight():
    with pytest.raises(ValidationError):
        request(questions={})
    with pytest.raises(ValidationError):
        request(questions={f"q{i}": NOUL for i in range(9)})
    assert len(request(questions={f"q{i}": NOUL for i in range(8)}).questions) == 8


def test_state_over_64_kib_is_rejected():
    limit = 64 * 1024
    # state_bytes counts the JSON encoding, which adds two quote characters to a string.
    assert request(state="x" * (limit - 2)).state_bytes() == limit
    with pytest.raises(ValidationError):
        request(state="x" * (limit - 1))


# --- decisions-api.dev adapter ---


def test_decisions_api_sends_documented_request_and_parses_answers(server):
    srv = server([envelope(GOOD)])
    d = DecisionsApiAdapter(jev_profile(srv.url), CREDS).decide(request(), timeout_seconds=5)
    assert d.answers["lane"] == Answer(
        type="choice", value="omega", confidence=0.8, probabilities={"alpha": 0.2, "omega": 0.8}
    )
    assert d.answers["risk"] == Answer(
        type="score", value=1.25, confidence=0.5, probabilities={"0": 0.0, "1": 0.75, "2": 0.25}
    )
    assert d.answers["urgent"] == Answer(type="noul", value=0.91)
    assert (d.input_tokens, d.output_tokens, d.model) == (30, 4, "typesafe/jev-1.13")
    path, headers, body = srv.requests[0]
    assert path == "/v1/systemone"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", headers["idempotency-key"])
    assert body == {"model": "typesafe/jev-1.13", "state": STATE, "questions": QUESTIONS}


def test_each_decide_gets_a_fresh_idempotency_key(server):
    srv = server([envelope(GOOD), envelope(GOOD)])
    adapter = DecisionsApiAdapter(jev_profile(srv.url), CREDS)
    adapter.decide(request(), timeout_seconds=5)
    adapter.decide(request(), timeout_seconds=5)
    assert srv.requests[0][1]["idempotency-key"] != srv.requests[1][1]["idempotency-key"]


def test_missing_usage_parses_as_none(server):
    srv = server([envelope(GOOD, usage={})])
    d = DecisionsApiAdapter(jev_profile(srv.url), CREDS).decide(request(), timeout_seconds=5)
    assert (d.input_tokens, d.output_tokens) == (None, None)


def test_http_error_status_is_reported_without_body(server):
    srv = server([(402, {"error": "secret echo"})])
    with pytest.raises(ModelHTTPError, match="http_error") as exc:
        DecisionsApiAdapter(jev_profile(srv.url), CREDS).decide(request(), timeout_seconds=5)
    assert exc.value.status == 402 and "secret" not in str(exc.value)


def test_requests_over_32_kib_are_refused_before_sending(server):
    srv = server([])
    with pytest.raises(ModelHTTPError, match="request_too_large"):
        DecisionsApiAdapter(jev_profile(srv.url), CREDS).decide(request(state="x" * (40 * 1024)), timeout_seconds=5)
    assert srv.requests == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.pop("urgent"),
        lambda a: a.update(extra={"type": "noul", "noul": 0.1}),
        lambda a: a.update(urgent={"type": "choice", "choice": "alpha", "probabilities": {"alpha": 1.0}}),
        lambda a: a["lane"].update(choice="beta"),
        lambda a: a["risk"].update(score=3.0),
        lambda a: a["lane"].update(probabilities={"alpha": 1.5, "omega": 0.8}),
        lambda a: a["urgent"].update(noul=1.2),
    ],
    ids=[
        "missing",
        "extra",
        "wrong-type",
        "choice-not-in-criteria",
        "score-out-of-range",
        "probability-out-of-range",
        "noul-out-of-range",
    ],
)
def test_invalid_answers_fail_closed(server, mutate):
    answers = copy.deepcopy(GOOD)
    mutate(answers)
    srv = server([envelope(answers)])
    with pytest.raises(ModelHTTPError, match="invalid_decision"):
        DecisionsApiAdapter(jev_profile(srv.url), CREDS).decide(request(), timeout_seconds=5)


# --- Cloudflare Workers AI adapter (clef) ---


def test_workers_ai_url_auth_and_both_envelopes(server):
    srv = server([(200, {"result": CLEF, "success": True, "errors": [], "messages": []}), (200, CLEF)])
    adapter = WorkersAiDecisionAdapter(clef_profile(srv.url), CREDS)
    enveloped = adapter.decide(request(), timeout_seconds=5)
    bare = adapter.decide(request(), timeout_seconds=5)
    assert enveloped.answers == bare.answers
    assert (enveloped.input_tokens, enveloped.output_tokens, enveloped.model) == (12, 3, "clef-omni")
    path, headers, body = srv.requests[0]
    assert path == f"/client/v4/accounts/{ACCOUNT}/ai/run/@cf/cloudflare/clef-omni"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert body == {"model": "clef-omni", "state": STATE, "questions": QUESTIONS}


def test_workers_ai_success_false_is_a_provider_error(server):
    srv = server([(200, {"success": False, "errors": [{"code": 1000}], "result": CLEF})])
    with pytest.raises(ModelHTTPError, match="provider_error"):
        WorkersAiDecisionAdapter(clef_profile(srv.url), CREDS).decide(request(), timeout_seconds=5)


# --- egress ---


def test_host_resolving_to_metadata_address_is_refused_with_no_request(monkeypatch):
    sent = []

    class NoSend:
        def open(self, req, timeout=None):
            sent.append(req)
            raise AssertionError("no request should be sent")

    monkeypatch.setattr(http_mod, "opener", NoSend())

    def resolver(host, port, **_):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    adapter = DecisionsApiAdapter(
        jev_profile("https://decisions-api.dev"), CREDS, policy=EgressPolicy(resolver=resolver)
    )
    with pytest.raises(ModelHTTPError, match="egress_denied:forbidden_address"):
        adapter.decide(request(), timeout_seconds=5)
    assert sent == []


# --- decision run, end to end (API -> worker -> answers.json) ---


@pytest.fixture
def decisions(live_api, tmp_path, monkeypatch):
    """A decision profile in the live profile dir, a worker that uses FakeDecisionAdapter for decisions
    (and the live FakeAdapter for agents), and the live app's definitions pointed at the same dir."""
    profiles = tmp_path / "profiles"
    (profiles / "jev-decisions.yaml").write_text(JEV_YAML)
    monkeypatch.setattr(main.runs.definitions, "profiles", ModelProfileRegistry(profiles))
    fake = FakeDecisionAdapter()
    worker = Worker(
        LocalChannel(live_api.ledger, artifacts=main.artifacts),
        lambda p: live_api.adapter,
        worker_id="decide-1",
        decision_factory=lambda p: fake,
    )
    monkeypatch.setattr(live_api, "worker", worker)
    return _Decisions(fake=fake, client=MaefClient(live_api.transport), store=live_api.ledger)


class _Decisions:
    def __init__(self, fake, client, store):
        self.fake, self.client, self.store = fake, client, store


def _decide(decisions, state=STATE, questions=QUESTIONS):
    return decisions.client.decide("jev-decisions", state, questions)["run_id"]


def _admitted_request_bytes(run):
    return next(e["payload"]["request_bytes"] for e in run["events"] if e["type"] == "task.admitted")


def test_decision_run_succeeds_with_answers_artifact(decisions, live_api):
    decisions.fake.answers = GOOD
    run_id = _decide(decisions)
    assert live_api.finish_queued()
    run = decisions.client.get_run(run_id)
    assert (run["kind"], run["status"], run["terminal_reason"]) == ("decision", "succeeded", "completed")
    assert [a["name"] for a in decisions.client.artifacts(run_id)] == ["answers.json"]
    assert decisions.client.answers(run_id) == {"answers": EXPECTED}


def test_events_carry_names_and_sizes_not_state_or_answers(decisions, live_api):
    decisions.fake.answers = GOOD
    run_id = _decide(decisions)
    live_api.finish_queued()
    events = decisions.client.get_run(run_id)["events"]
    request_event = next(e for e in events if e["type"] == "decision.request")
    assert request_event["payload"] == {
        "profile": "jev-decisions",
        "questions": list(QUESTIONS),
        "state_bytes": len(json.dumps(STATE, ensure_ascii=False).encode()),
    }
    text = json.dumps(events)
    assert STATE not in text and "omega" not in text


def test_reported_usage_is_charged(decisions, live_api):
    run_id = _decide(decisions)
    live_api.finish_queued()
    assert decisions.client.get_run(run_id)["tokens_used"] == 110  # fake reports 100 input + 10 output
    assert decisions.store.account("default")["spent_tokens"] == 110
    assert decisions.store.account("default")["reserved_tokens"] == 0


def test_unreported_usage_charges_request_bytes(decisions, live_api):
    decisions.fake.input_tokens = decisions.fake.output_tokens = None
    run_id = _decide(decisions)
    live_api.finish_queued()
    run = decisions.client.get_run(run_id)
    assert run["status"] == "succeeded"
    assert run["tokens_used"] == _admitted_request_bytes(run)
    assert any(e["type"] == "usage.recorded" and e["payload"]["reported"] is False for e in run["events"])


def test_invalid_decision_fails_and_charges_request_bytes(decisions, live_api):
    decisions.fake.error = ModelHTTPError(None, "invalid_decision")
    run_id = _decide(decisions)
    live_api.finish_queued()
    run = decisions.client.get_run(run_id)
    assert run["status"] == "failed"
    assert run["terminal_reason"] == "invalid_decision"
    assert run["tokens_used"] == _admitted_request_bytes(run)
    assert decisions.client.answers(run_id) is None


def test_profile_must_exist_and_be_a_decision_model(decisions):
    with pytest.raises(MaefClientError) as bad_dialect:
        decisions.client.decide("local-default", STATE, QUESTIONS)
    assert (bad_dialect.value.status, bad_dialect.value.code) == (409, "not_a_decision_profile")
    with pytest.raises(MaefClientError) as missing:
        decisions.client.decide("no-such-profile", STATE, QUESTIONS)
    assert missing.value.status == 404


def test_agent_run_on_a_decision_profile_is_not_implemented(decisions, monkeypatch):
    pinned = definition(dirty=True)
    jev = ModelEndpointProfile.model_validate(yaml.safe_load(JEV_YAML))
    monkeypatch.setattr(main.runs.definitions, "resolve", lambda agent_id: pinned.model_copy(update={"profile": jev}))
    with pytest.raises(MaefClientError) as exc:
        decisions.client.submit("research-analyst", "Hello")
    assert exc.value.status == 501


def test_cancel_before_claim_settles_with_no_tokens(decisions, live_api):
    run_id = _decide(decisions)
    assert decisions.client.cancel(run_id)["outcome"] == "cancelled"
    assert live_api.finish_queued() is False
    run = decisions.client.get_run(run_id)
    assert (run["status"], run["tokens_used"], run["terminal_reason"]) == ("cancelled", 0, "cancelled_by_user")
    assert decisions.store.account("default")["spent_tokens"] == 0


# --- client, CLI and gateway ---


def test_client_decide_then_answers(decisions, live_api):
    run = decisions.client.decide("jev-decisions", STATE, QUESTIONS)
    assert run["status"] == "admitted"
    assert decisions.client.answers(run["run_id"]) is None
    live_api.finish_queued()
    answers = decisions.client.answers(run["run_id"])["answers"]
    assert set(answers) == set(QUESTIONS)
    assert answers["urgent"] == {"type": "noul", "value": 0.5}  # the fake's default for noul


def test_cli_decide_waits_and_prints_answers(decisions, live_api, capsys):
    live_api.finish_later(0.1)
    code = cli(
        live_api,
        "decide",
        "jev-decisions",
        "--state",
        STATE,
        "--question",
        "urgent:noul:Is it urgent",
        "--wait",
        "--timeout",
        "5",
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"answers": {"urgent": {"type": "noul", "value": 0.5}}}


def test_gateway_decide_returns_wrapped_answers(decisions, live_api):
    live_api.finish_later(0.1)
    result = call(
        decisions.client, "decide", profile_id="jev-decisions", state=STATE, questions={"urgent": NOUL}, wait_seconds=5
    )
    text = text_of(result)
    run_id = re.search(r"run_id=(\S+)", text).group(1)
    assert "isError" not in result
    assert "status=succeeded" in text.splitlines()[0]
    assert wrapped(run_id, json.dumps(decisions.client.answers(run_id), indent=2)) in text


def test_gateway_decide_api_error_is_a_tool_error(decisions):
    result = call(decisions.client, "decide", profile_id="no-such-profile", state=STATE, questions={"urgent": NOUL})
    assert result["isError"] is True
    assert text_of(result) == "Model profile not found (http_error)"
