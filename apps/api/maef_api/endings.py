"""Terminal reasons and how a run ended.

A run ends one of two ways. An `agent` ending is the agent's own outcome: it finished, ran out of
budget, was cancelled, or produced something the harness cannot continue from. An `infrastructure`
ending is a loss: the platform failed the run (lost worker, provider outage, sandbox or artifact
store fault, internal bug) and the result says nothing about the agent. Benchmarks list losses
separately and keep them out of the score.

Every terminal reason used in code is listed here exactly once, by its base: the text before the
first `:` (`setup_error:RuntimeError` has the base `setup_error`). A test checks that the code and
this table agree.
"""

from typing import Literal

Ending = Literal["agent", "infrastructure"]

AGENT_REASONS: frozenset[str] = frozenset(
    {
        "completed",  # succeeded
        "output_truncated",  # the model hit its output cap
        "tokens_exhausted",
        "turns_exhausted",
        "duration_exhausted",
        "cancelled_by_user",
        "malformed_response",  # tool_use stop without tool calls
        "unsupported_stop_reason",  # unsupported_stop_reason:<x>
        "invalid_decision",  # a decision model answered outside the schema
        "output_rejected",  # output_rejected:<code>, the answer could not be stored (size or limit)
        "network_not_permitted",  # the agent definition asks for network it may not have
    }
)

INFRASTRUCTURE_REASONS: frozenset[str] = frozenset(
    {
        "worker_lost",  # lease expired or the worker gave up on the control plane
        "provider_error",  # model endpoint failed after the adapter's own retries
        "sandbox_error",
        "artifact_error",  # artifact store failed (for example ENOSPC)
        "internal_error",
        "sandbox_unavailable",  # no usable OCI runtime or sandbox profile at start
        "mcp_unavailable",  # mcp_unavailable:<server>
        "credential_missing",  # credential_missing:<ENV>
        "credential_not_allowed",  # credential_not_allowed:<ENV> (worker credential allowlist)
        "mcp_manifest_mismatch",  # mcp_manifest_mismatch:<server>, lease manifest != the worker's local file
        "setup_error",  # setup_error:<Class>
        "definition_mismatch",  # the pinned bytes did not match their digest
        "api_error",  # api_error:<code>, a client could not read the run (evals)
        "eval_timeout",  # the eval runner stopped waiting for the run (evals)
    }
)

TERMINAL_REASONS: dict[str, Ending] = {
    **dict.fromkeys(AGENT_REASONS, "agent"),
    **dict.fromkeys(INFRASTRUCTURE_REASONS, "infrastructure"),
}


def reason_base(reason: str) -> str:
    return reason.split(":", 1)[0]


def ending_of(reason: str | None) -> Ending | None:
    """The class of a terminal reason; None while the run has no reason. An unmapped reason is treated
    as an infrastructure loss (it was not the agent's doing and must not count against it)."""
    if not reason:
        return None
    return TERMINAL_REASONS.get(reason_base(reason), "infrastructure")
