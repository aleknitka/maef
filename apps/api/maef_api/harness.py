"""Harness: the turn loop. One bounded model call per turn, tool calls executed between turns.

The BudgetGuard is consulted before every call. Prompts, tool arguments and tool results are
never logged: events carry sizes and counts only. The model's final answer is stored once, in
the `run.output` event.
"""
import json
import time
from dataclasses import dataclass
from typing import Protocol

from .adapters.base import Message, ModelAdapter, ToolCall, ToolSpec
from .definitions import PinnedDefinition
from .guard import BudgetGuard
from .ledger import Ledger
from .models import Budget, ResultManifest
from .tools import BuiltinTools, ToolResult, ToolSet, spec_bytes


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    pinned: PinnedDefinition
    budget: Budget
    reserved_tokens: int
    instruction: str
    tools: ToolSet | None = None  # None: no tools


class SinkFailure(Exception):
    """The sink can no longer accept this run's events (lease lost, sequence broken, control
    plane unreachable). The run is abandoned: execute() re-raises it without trying to finish."""


class RunSink(Protocol):
    """Where a run's events and its terminal result go. The seam between harness and ledger."""

    def append(self, type: str, payload: dict | None = None, *, status: str | None = None) -> None: ...

    def complete(self, *, tokens_used: int, status: str, terminal_reason: str) -> None: ...


class LedgerSink:
    """In-process sink: writes straight to the ledger (tests, single-process use)."""

    def __init__(self, ledger: Ledger, run_id: str):
        self._ledger, self._run_id = ledger, run_id

    def append(self, type: str, payload: dict | None = None, *, status: str | None = None) -> None:
        self._ledger.append(self._run_id, type, payload, status=status)

    def complete(self, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._ledger.settle(self._run_id, tokens_used=tokens_used, status=status, terminal_reason=terminal_reason)


def _size(value: object) -> int:
    return len(json.dumps(value, default=str).encode())


def _prompt_bytes(system: str, messages: list[Message], specs: list[ToolSpec]) -> int:
    """Bytes bound tokens. Must equal the admission estimate for the first turn."""
    total = len(system.encode()) + spec_bytes(specs)
    for m in messages:
        total += len(m.text.encode()) + sum(len(c.name.encode()) + _size(c.arguments) for c in m.tool_calls)
    return total


def _invoke(tools: ToolSet, call: ToolCall) -> ToolResult:
    if call.parse_error:
        return ToolResult(call.parse_error, True)
    return tools.invoke(call.name, call.arguments)


def execute(sink: RunSink, run: PreparedRun, adapter: ModelAdapter, clock=time.monotonic) -> None:
    """Run one prepared run to a terminal state. Model and tool failures are recorded, not raised;
    only SinkFailure escapes (the run was abandoned)."""
    pinned, profile = run.pinned, run.pinned.profile
    tools = run.tools or BuiltinTools([], {})
    specs = tools.specs()
    guard = BudgetGuard(run.budget, run.reserved_tokens, profile.max_output_tokens, clock)
    in_tokens = out_tokens = 0
    status, reason = "failed", "internal_error"
    messages = [Message("user", run.instruction)]
    try:
        sink.append("run.started", status="running")
        while True:
            prompt_bytes = _prompt_bytes(pinned.instructions, messages, specs)
            stop = guard.check(prompt_bytes)
            if stop:
                reason = stop
                break
            turn = guard.turns + 1
            sink.append("model.request", {
                "turn": turn, "profile": profile.id, "prompt_bytes": prompt_bytes,
                "max_output_tokens": profile.max_output_tokens, "tools": len(specs)})
            response = adapter.converse(
                system=pinned.instructions, messages=list(messages), tools=specs,
                max_output_tokens=profile.max_output_tokens,
                timeout_seconds=min(float(profile.timeout_seconds), guard.remaining_seconds()),
            )
            guard.record(response.input_tokens, response.output_tokens)
            in_tokens += response.input_tokens
            out_tokens += response.output_tokens
            sink.append("model.response", {
                "turn": turn, "stop_reason": response.stop_reason, "tool_calls": len(response.tool_calls)})
            sink.append("usage.recorded", {
                "turn": turn, "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                "total_tokens": response.input_tokens + response.output_tokens})
            messages.append(Message("assistant", response.text, response.tool_calls))

            if response.stop_reason == "tool_use":
                if not response.tool_calls:
                    reason = "malformed_response"
                    break
                for call in response.tool_calls:
                    sink.append("tool.call", {
                        "turn": turn, "name": call.name, "argument_bytes": _size(call.arguments)})
                    result = _invoke(tools, call)
                    sink.append("tool.result", {
                        "turn": turn, "name": call.name, "result_bytes": len(result.content.encode()),
                        "is_error": result.is_error})
                    messages.append(Message("tool", result.content, tool_call_id=call.id, is_error=result.is_error))
                continue

            sink.append("run.output", {"text": response.text})
            if response.stop_reason == "end_turn":
                status, reason = "succeeded", "completed"
            elif response.stop_reason == "max_tokens":
                reason = "output_truncated"
            else:
                reason = f"unsupported_stop_reason:{response.stop_reason}"
            break
    except SinkFailure:
        raise
    except Exception as exc:  # noqa: BLE001  # recorded by class name only; messages may echo prompts
        status, reason = "failed", f"model_error:{type(exc).__name__}"

    # Usage is what the provider reported. A call that errored reported nothing, so it is
    # charged 0 and the reservation is released (known gap: a timeout may still consume upstream).
    # The manifest is written before settle(), whose task.* event is the terminal one.
    manifest = ResultManifest(
        run_id=run.run_id, status=status, agent_id=pinned.agent.id,
        agent_revision=pinned.git_rev, dirty=pinned.dirty,
        definition_digest=pinned.definition_digest, model_profile_revision=pinned.profile_digest,
        input_tokens=in_tokens, output_tokens=out_tokens, tokens_used=guard.tokens_used, terminal_reason=reason,
    )
    sink.append("run.manifest", manifest.model_dump(mode="json"))
    sink.complete(tokens_used=guard.tokens_used, status=status, terminal_reason=reason)
