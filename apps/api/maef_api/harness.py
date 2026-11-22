"""Minimal tool-less harness: one bounded model call per run.

A tool-less agent finishes in a single turn, so there is no turn loop yet; max_turns
becomes meaningful once tools exist. Prompts are not logged: events carry sizes and
token counts only. The model's answer is stored once, in the `run.output` event.
"""
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING

from .adapters.base import ModelAdapter
from .admission import settle
from .models import (
    Budget,
    ModelEndpointProfile,
    PriceSchedule,
    ResolvedDefinition,
    ResultManifest,
)
from .store import Store, append_event


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    definition: ResolvedDefinition
    profile: ModelEndpointProfile
    budget: Budget
    reserved_micro: int
    system_prompt: str
    instruction: str


def call_cost_micro(price: PriceSchedule, input_tokens: int, output_tokens: int) -> int:
    # USD per million tokens x tokens = micro-USD
    exact = price.input_usd_per_million_tokens * input_tokens + price.output_usd_per_million_tokens * output_tokens
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


def _event(store: Store, run_id: str, type_: str, payload: dict, status: str | None = None) -> None:
    with store.transaction() as conn:
        if status:
            conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        append_event(conn, run_id, type_, payload)


def execute(store: Store, run: PreparedRun, adapter: ModelAdapter, clock=time.monotonic) -> None:
    """Run one prepared run to a terminal state. Never raises: failures are recorded."""
    started = clock()
    price = run.definition.price_schedule
    cost = in_tokens = out_tokens = 0
    status, reason = "failed", "internal_error"
    try:
        _event(store, run.run_id, "run.started", {}, status="running")
        prompt_bytes = len(run.system_prompt.encode()) + len(run.instruction.encode())

        # Worst case for this call must fit in the reservation (prompt bytes bound tokens).
        worst = call_cost_micro(price, prompt_bytes, run.profile.max_output_tokens)
        remaining = float(run.budget.max_duration_seconds) - (clock() - started)
        if worst > run.reserved_micro:
            reason = "budget_exhausted"
        elif remaining <= 0:
            reason = "duration_exhausted"
        else:
            _event(store, run.run_id, "model.request", {"profile": run.profile.id, "prompt_bytes": prompt_bytes,
                                                         "max_output_tokens": run.profile.max_output_tokens})
            response = adapter.converse(
                system=run.system_prompt, user=run.instruction,
                max_output_tokens=run.profile.max_output_tokens,
                timeout_seconds=min(float(run.profile.timeout_seconds), remaining),
            )
            in_tokens, out_tokens = response.input_tokens, response.output_tokens
            cost = call_cost_micro(price, in_tokens, out_tokens)
            _event(store, run.run_id, "model.response", {"stop_reason": response.stop_reason})
            _event(store, run.run_id, "usage.recorded", {"input_tokens": in_tokens, "output_tokens": out_tokens, "cost_micro_usd": cost})
            _event(store, run.run_id, "run.output", {"text": response.text})
            if response.stop_reason == "end_turn":
                status, reason = "succeeded", "completed"
            elif response.stop_reason == "max_tokens":
                reason = "output_truncated"
            else:
                reason = f"unsupported_stop_reason:{response.stop_reason}"
    except Exception as exc:  # recorded by class name only; messages may echo prompts
        status, reason = "failed", f"model_error:{type(exc).__name__}"

    # Cost is what the provider reported. A call that errored reported nothing, so it is
    # charged 0 and the reservation is released (known gap: a timeout may still bill upstream).
    # The manifest is written before settle(), whose task.* event is the terminal one.
    manifest = ResultManifest(
        run_id=run.run_id, status=status, agent_id=run.definition.agent_id,
        agent_revision=run.definition.git_rev, dirty=run.definition.dirty,
        definition_digest=run.definition.definition_digest, model_profile_revision=run.definition.profile_digest,
        input_tokens=in_tokens, output_tokens=out_tokens, cost_micro_usd=cost, terminal_reason=reason,
    )
    _event(store, run.run_id, "run.manifest", manifest.model_dump(mode="json"))
    settle(store, run.run_id, cost_micro=cost, status=status, terminal_reason=reason)
