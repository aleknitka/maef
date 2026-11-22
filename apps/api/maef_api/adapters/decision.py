"""Decision models: typed questions about a `state` instead of generated text (see docs/DECISION_MODELS.md).

`DecisionAdapter.decide(request, timeout_seconds=...)` is one non-streaming call. Both wire formats
(decisions-api.dev, Cloudflare Workers AI) share the System One question and answer shapes; each
adapter wraps them differently. Responses are validated strictly and failures never echo bodies.
"""
import json
import math
import uuid
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..credentials import CredentialError, Credentials
from ..egress import EgressPolicy
from ..models import DecisionsApiEndpoint, ModelEndpointProfile, WorkersAiEndpoint
from .http import ModelHTTPError, post_json

MAX_QUESTIONS = 8
MAX_STATE_BYTES = 64 * 1024
MAX_INSTRUCTIONS = 2000
CHOICE_OPTIONS = (2, 20)
SCORE_LEVELS = (2, 10)
# decisions-api.dev caps text plus questions at 32 KiB (images excluded).
DECISIONS_API_MAX_BYTES = 32 * 1024
_EPS = 1e-6

QuestionName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,40}$")]
_Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["choice", "score", "noul"]
    instructions: str = Field(min_length=1, max_length=MAX_INSTRUCTIONS)
    # choice: {option: description}; score: ordered level descriptions, lowest first (level = index);
    # noul: optional {"true": ..., "false": ...}.
    criteria: dict[Annotated[str, StringConstraints(min_length=1, max_length=64)], _Text] | list[_Text] | None = None

    @model_validator(mode="after")
    def _criteria_fit_type(self):
        c = self.criteria
        if self.type == "choice":
            if not isinstance(c, dict) or not CHOICE_OPTIONS[0] <= len(c) <= CHOICE_OPTIONS[1]:
                raise ValueError(f"choice needs criteria: {CHOICE_OPTIONS[0]}-{CHOICE_OPTIONS[1]} options as {{option: description}}")
        elif self.type == "score":
            if not isinstance(c, list) or not SCORE_LEVELS[0] <= len(c) <= SCORE_LEVELS[1]:
                raise ValueError(f"score needs criteria: {SCORE_LEVELS[0]}-{SCORE_LEVELS[1]} ordered level descriptions")
        elif c is not None and (not isinstance(c, dict) or not set(c) <= {"true", "false"}):
            raise ValueError('noul criteria may only be {"true": ..., "false": ...}')
        return self

    def wire(self) -> dict:
        out: dict = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = self.criteria
        return out


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str | dict | list
    questions: dict[QuestionName, Question] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def _state_size(self):
        if self.state_bytes() > MAX_STATE_BYTES:
            raise ValueError(f"state exceeds {MAX_STATE_BYTES // 1024} KiB")
        return self

    def state_bytes(self) -> int:
        return len(json.dumps(self.state, ensure_ascii=False).encode())

    def wire_questions(self) -> dict:
        return {name: q.wire() for name, q in self.questions.items()}

    def request_bytes(self) -> int:
        """Serialized state plus questions: bounds input tokens (a token is at least one byte)."""
        return len(json.dumps({"state": self.state, "questions": self.wire_questions()}, ensure_ascii=False).encode())


class DecisionRunRequest(DecisionRequest):
    """Body of POST /api/v1/decisions."""
    profile_id: str
    max_tokens: int | None = Field(default=None, ge=1, le=100_000_000)


class Answer(BaseModel):
    type: Literal["choice", "score", "noul"]
    # choice: the chosen option; score: probability-weighted level (0 = lowest, may be fractional);
    # noul: probability of yes, 0..1.
    value: str | float
    confidence: float | None = None
    probabilities: dict[str, float] | None = None


class Decision(BaseModel):
    answers: dict[str, Answer]
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


class DecisionAdapter(Protocol):
    def decide(self, req: DecisionRequest, *, timeout_seconds: float) -> Decision:
        """One call. Raises ModelHTTPError on transport, service or validation (`invalid_decision`) errors."""
        ...


def _invalid() -> ModelHTTPError:
    return ModelHTTPError(None, "invalid_decision")


def _num(v, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise _invalid()
    if (lo is not None and v < lo - _EPS) or (hi is not None and v > hi + _EPS):
        raise _invalid()
    return float(v)


def _probabilities(raw, allowed: set[str]) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not set(raw) <= allowed:
        raise _invalid()
    return {k: _num(v, 0, 1) for k, v in raw.items()}


def parse_answers(req: DecisionRequest, raw: object) -> dict[str, Answer]:
    """Every requested question answered (and nothing else), each within its question's domain."""
    if not isinstance(raw, dict) or set(raw) != set(req.questions):
        raise _invalid()
    out: dict[str, Answer] = {}
    for name, q in req.questions.items():
        a = raw[name]
        if not isinstance(a, dict) or a.get("type") != q.type:
            raise _invalid()
        confidence = None if a.get("confidence") is None else _num(a["confidence"], 0, 1)
        if q.type == "noul":
            out[name] = Answer(type="noul", value=_num(a.get("noul"), 0, 1), confidence=confidence)
        elif q.type == "choice":
            options = set(q.criteria)
            choice = a.get("choice")
            if not isinstance(choice, str) or choice not in options:
                raise _invalid()
            out[name] = Answer(type="choice", value=choice, confidence=confidence,
                               probabilities=_probabilities(a.get("probabilities"), options))
        else:
            levels = len(q.criteria)
            out[name] = Answer(type="score", value=_num(a.get("score"), 0, levels - 1), confidence=confidence,
                               probabilities=_probabilities(a.get("probabilities"), {str(i) for i in range(levels)}))
    return out


def _tokens(usage: object, key: str) -> int | None:
    v = usage.get(key) if isinstance(usage, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _decision(req: DecisionRequest, result: object) -> Decision:
    if not isinstance(result, dict):
        raise _invalid()
    model = result.get("model")
    return Decision(answers=parse_answers(req, result.get("answers")),
                    input_tokens=_tokens(result.get("usage"), "input_tokens"),
                    output_tokens=_tokens(result.get("usage"), "output_tokens"),
                    model=model[:200] if isinstance(model, str) else None)


class _HttpDecisionAdapter:
    _endpoint_type: type

    def __init__(self, profile: ModelEndpointProfile, credentials: Credentials, sleep=None,
                 policy: EgressPolicy | None = None):
        if not isinstance(profile.endpoint, self._endpoint_type):
            raise TypeError(f"Profile {profile.id} is not a {self._endpoint_type.__name__} profile")
        self.profile = profile
        self._credentials = credentials
        self._policy = policy or EgressPolicy()
        self._extra = {"sleep": sleep} if sleep else {}

    def _key(self) -> str:
        key = self._credentials.resolve(self.profile.credential_ref)
        if key is None:
            raise CredentialError("missing_credential:decision")
        return key.reveal()

    def _post(self, url: str, headers: dict, body: dict, timeout_seconds: float) -> dict:
        ep = self.profile.endpoint
        reply = post_json(url, headers, body, timeout=timeout_seconds, policy=self._policy,
                          allow_loopback=ep.is_loopback, allow_private=ep.allow_private_network,
                          max_retries=self.profile.max_retries, **self._extra)
        if not isinstance(reply, dict):
            raise _invalid()
        return reply


class DecisionsApiAdapter(_HttpDecisionAdapter):
    """decisions-api.dev `POST /v1/systemone` (Jev). Answers are at data.result.answers when code == 0."""
    _endpoint_type = DecisionsApiEndpoint

    def decide(self, req: DecisionRequest, *, timeout_seconds: float) -> Decision:
        ep = self.profile.endpoint
        if req.request_bytes() > DECISIONS_API_MAX_BYTES:
            raise ModelHTTPError(None, "request_too_large")
        # One key per logical request; a retry after a failure may be answered 409 (fails closed, no double charge).
        headers = {"Authorization": f"Bearer {self._key()}", "Idempotency-Key": uuid.uuid4().hex}
        body = {"model": ep.model_id, "state": req.state, "questions": req.wire_questions()}
        reply = self._post(f"{ep.base_url}/v1/systemone", headers, body, timeout_seconds)
        data = reply.get("data")
        if reply.get("code") != 0 or not isinstance(data, dict):
            raise ModelHTTPError(None, "provider_error")
        return _decision(req, data.get("result"))


class WorkersAiDecisionAdapter(_HttpDecisionAdapter):
    """Cloudflare Workers AI clef models: `POST /accounts/{id}/ai/run/@cf/cloudflare/clef-omni`.
    The REST envelope {result, success, errors, messages} is unwrapped when present."""
    _endpoint_type = WorkersAiEndpoint

    def decide(self, req: DecisionRequest, *, timeout_seconds: float) -> Decision:
        ep = self.profile.endpoint
        headers = {"Authorization": f"Bearer {self._key()}"}
        # The body selects the model by its short name ("clef-omni" for @cf/cloudflare/clef-omni).
        body = {"model": ep.model_id.rsplit("/", 1)[-1], "state": req.state, "questions": req.wire_questions()}
        reply = self._post(f"{ep.base_url}/accounts/{ep.account_id}/ai/run/{ep.model_id}", headers, body, timeout_seconds)
        if "result" in reply:
            if reply.get("success") is not True:
                raise ModelHTTPError(None, "provider_error")
            reply = reply["result"]
        return _decision(req, reply)


class FakeDecisionAdapter:
    """Deterministic stand-in: first option / level 0 / 0.5. Set `answers` (name -> raw answer dict) or `error`."""

    def __init__(self, input_tokens: int | None = 100, output_tokens: int | None = 10):
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.answers: dict | None = None
        self.error: Exception | None = None
        self.calls: list[DecisionRequest] = []

    def decide(self, req: DecisionRequest, *, timeout_seconds: float) -> Decision:
        self.calls.append(req)
        if self.error is not None:
            raise self.error
        raw = self.answers
        if raw is None:
            raw = {}
            for name, q in req.questions.items():
                if q.type == "choice":
                    first = next(iter(q.criteria))
                    raw[name] = {"type": "choice", "choice": first, "confidence": 1.0,
                                 "probabilities": {k: 1.0 if k == first else 0.0 for k in q.criteria}}
                elif q.type == "score":
                    raw[name] = {"type": "score", "score": 0.0, "confidence": 1.0}
                else:
                    raw[name] = {"type": "noul", "noul": 0.5}
        return Decision(answers=parse_answers(req, raw), input_tokens=self.input_tokens,
                        output_tokens=self.output_tokens, model="fake")
