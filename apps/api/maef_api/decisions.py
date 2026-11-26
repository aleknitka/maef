"""Decision run executor: one bounded decide() call, answers stored as the `answers.json` artifact.

Events carry names and sizes only: state and answers are untrusted content and never appear in them.
Usage is what the provider reported; when it reports none, the request bytes are charged as input
tokens (a token is at least one byte), so an unmetered response cannot be free.
"""

import json
from collections.abc import Callable

from .adapters.base import model_error_reason
from .adapters.decision import DecisionAdapter
from .adapters.http import ModelHTTPError
from .artifacts import ArtifactRejected
from .harness import RunSink, SinkFailure
from .leases import TaskPayload
from .models import ResultManifest

ANSWERS_NAME = "answers.json"


def execute_decision(
    sink: RunSink, run_id: str, task: TaskPayload, adapter: DecisionAdapter, cancelled: Callable[[], bool] | None = None
) -> None:
    """Run one decision to a terminal state. Failures are recorded, not raised; only SinkFailure escapes."""
    d = task.decision
    if d is None:  # TaskPayload validates this for kind == "decision"
        raise ValueError("decision task has no decision payload")
    profile, request = d.profile, d.request
    in_tokens = out_tokens = 0
    artifacts = []
    status, reason = "failed", "internal_error"
    try:
        sink.append("run.started", status="running")
        if cancelled is not None and cancelled():
            status, reason = "cancelled", "cancelled_by_user"
        else:
            sink.append(
                "decision.request",
                {"profile": profile.id, "questions": list(request.questions), "state_bytes": request.state_bytes()},
            )
            try:
                decision = adapter.decide(request, timeout_seconds=float(profile.timeout_seconds))
            except ModelHTTPError as exc:
                if str(exc).startswith("invalid_decision"):
                    in_tokens = request.request_bytes()  # the provider answered (and likely billed): fail closed
                    sink.append(
                        "usage.recorded",
                        {
                            "turn": 1,
                            "input_tokens": in_tokens,
                            "output_tokens": 0,
                            "total_tokens": in_tokens,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                        },
                    )
                raise
            in_tokens = decision.input_tokens if decision.input_tokens is not None else request.request_bytes()
            out_tokens = decision.output_tokens or 0
            sink.append(
                "usage.recorded",
                {
                    "turn": 1,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "total_tokens": in_tokens + out_tokens,
                    "reported": decision.input_tokens is not None,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )
            body = json.dumps(
                {"answers": {n: a.model_dump(exclude_none=True) for n, a in decision.answers.items()}}, indent=2
            ).encode()
            try:
                ref = sink.upload(ANSWERS_NAME, "application/json", body)
                artifacts.append(ref)
                sink.append("run.output", ref.model_dump())
                status, reason = "succeeded", "completed"
            except ArtifactRejected as exc:
                sink.append("run.output", {"error": exc.code, "bytes": len(body)})
                reason = f"output_rejected:{exc.code}"
    except SinkFailure:
        raise
    except ModelHTTPError as exc:
        # The kind is a fixed code (never provider text), so it is safe to record.
        status = "failed"
        reason = "invalid_decision" if str(exc).startswith("invalid_decision") else model_error_reason(exc)
    except Exception as exc:  # noqa: BLE001  # class name only; messages may echo prompts or keys
        status, reason = "failed", model_error_reason(exc)
    used = in_tokens + out_tokens
    manifest = ResultManifest(
        run_id=run_id,
        status=status,
        agent_id=f"decision:{profile.id}",
        agent_revision=None,
        dirty=False,
        definition_digest=d.profile_digest,
        model_profile_revision=d.profile_digest,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        tokens_used=used,
        terminal_reason=reason,
        artifacts=artifacts,
    )
    sink.append("run.manifest", manifest.model_dump(mode="json"))
    sink.complete(tokens_used=used, status=status, terminal_reason=reason)
