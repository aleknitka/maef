"""Harness: the turn loop. One bounded model call per turn, tool calls executed between turns.

The BudgetGuard is consulted before every call. Prompts, tool arguments and tool results are
never logged: events carry sizes and counts only. The model's final answer is stored once, as the
`output.md` artifact; `run.output` carries only its ref. Sandbox files under /workspace/out/ are
collected as artifacts after the loop and listed in the manifest.
"""
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .adapters.base import Message, ModelAdapter, ToolCall, ToolSpec, model_error_reason
from .artifacts import (
    ArtifactIntake,
    ArtifactRef,
    ArtifactRejected,
    ArtifactStore,
    InMemoryArtifactStore,
)
from .definitions import PinnedDefinition
from .guard import BudgetGuard
from .ledger import Ledger
from .models import Budget, ResultManifest
from .outputs import Collected
from .sandbox import SandboxError
from .tools import BuiltinTools, ToolResult, ToolSet, spec_bytes


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    pinned: PinnedDefinition
    budget: Budget
    reserved_tokens: int
    instruction: str
    tools: ToolSet | None = None  # None: no tools
    outputs: Callable[[], Collected] | None = None  # sandbox file collection; None: no sandbox session
    cancelled: Callable[[], bool] | None = None  # True once the user asked to cancel; polled between steps


class SinkFailure(Exception):
    """The sink can no longer accept this run's events (lease lost, sequence broken, control
    plane unreachable). The run is abandoned: execute() re-raises it without trying to finish."""


class RunSink(Protocol):
    """Where a run's events and its terminal result go. The seam between harness and ledger."""

    def append(self, type: str, payload: dict | None = None, *, status: str | None = None) -> None: ...

    def upload(self, name: str, mime: str, data: bytes) -> ArtifactRef:
        """Store an artifact for this run. Raises ArtifactRejected (not retryable) or SinkFailure."""
        ...

    def complete(self, *, tokens_used: int, status: str, terminal_reason: str) -> None: ...


class LedgerSink:
    """In-process sink: writes straight to the ledger (tests, single-process use)."""

    def __init__(self, ledger: Ledger, run_id: str, artifacts: ArtifactStore | None = None):
        self._ledger, self._run_id = ledger, run_id
        self.artifacts = artifacts or InMemoryArtifactStore()
        self._intake = ArtifactIntake(ledger, self.artifacts)

    def upload(self, name: str, mime: str, data: bytes) -> ArtifactRef:
        return self._intake.accept(self._run_id, token=None, name=name, mime=mime, data=data)

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


def _collect_outputs(sink: RunSink, collect: Callable[[], Collected], artifacts: list[ArtifactRef]) -> None:
    """Best effort: a collection problem is an event, never a failed run. Only SinkFailure escapes."""
    try:
        collected = collect()
    except Exception as exc:  # noqa: BLE001  # code or class name only
        code = str(exc) if isinstance(exc, SandboxError) else type(exc).__name__
        sink.append("outputs.collection_failed", {"code": code})
        return
    skipped, stored = list(collected.skipped), 0
    for file in collected.files:
        try:
            artifacts.append(sink.upload(file.name, file.mime, file.data))
            stored += 1
        except ArtifactRejected as exc:
            skipped.append({"name": file.name, "reason": exc.code})
    if stored or skipped:
        sink.append("outputs.collected", {"count": stored, "skipped": skipped})


def execute(sink: RunSink, run: PreparedRun, adapter: ModelAdapter, clock=time.monotonic) -> None:
    """Run one prepared run to a terminal state. Model and tool failures are recorded, not raised;
    only SinkFailure escapes (the run was abandoned)."""
    pinned, profile = run.pinned, run.pinned.profile
    tools = run.tools or BuiltinTools([], {})
    specs = tools.specs()
    server_of = getattr(tools, "server_of", None) or (lambda name: None)
    guard = BudgetGuard(run.budget, run.reserved_tokens, profile.max_output_tokens, clock, run.cancelled)
    in_tokens = out_tokens = 0
    artifacts: list[ArtifactRef] = []
    status, reason = "failed", "internal_error"
    messages = [Message("user", run.instruction)]
    try:
        sink.append("run.started", status="running")
        while True:
            prompt_bytes = _prompt_bytes(pinned.instructions, messages, specs)
            stop = guard.check(prompt_bytes)
            if stop:
                status, reason = ("cancelled", "cancelled_by_user") if stop == "cancelled" else ("failed", stop)
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
                    if guard.cancelled():
                        break  # skip the remaining calls; the next guard check ends the run
                    server = {"server": s} if (s := server_of(call.name)) else {}
                    sink.append("tool.call", {
                        "turn": turn, "name": call.name, "argument_bytes": _size(call.arguments), **server})
                    result = _invoke(tools, call)
                    sink.append("tool.result", {
                        "turn": turn, "name": call.name, "result_bytes": len(result.content.encode()),
                        "is_error": result.is_error, **server})
                    messages.append(Message("tool", result.content, tool_call_id=call.id, is_error=result.is_error))
                continue

            try:
                ref = sink.upload("output.md", "text/markdown", response.text.encode())
                artifacts.append(ref)
                sink.append("run.output", ref.model_dump())
            except ArtifactRejected as exc:
                sink.append("run.output", {"error": exc.code, "bytes": len(response.text.encode())})
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
        status, reason = "failed", model_error_reason(exc)

    if run.outputs is not None:
        _collect_outputs(sink, run.outputs, artifacts)

    # Usage is what the provider reported. A call that errored reported nothing, so it is
    # charged 0 and the reservation is released (known gap: a timeout may still consume upstream).
    # The manifest is written before settle(), whose task.* event is the terminal one.
    manifest = ResultManifest(
        run_id=run.run_id, status=status, agent_id=pinned.agent.id,
        agent_revision=pinned.git_rev, dirty=pinned.dirty,
        definition_digest=pinned.definition_digest, model_profile_revision=pinned.profile_digest,
        input_tokens=in_tokens, output_tokens=out_tokens, tokens_used=guard.tokens_used, terminal_reason=reason,
        artifacts=artifacts,
    )
    sink.append("run.manifest", manifest.model_dump(mode="json"))
    sink.complete(tokens_used=guard.tokens_used, status=status, terminal_reason=reason)
