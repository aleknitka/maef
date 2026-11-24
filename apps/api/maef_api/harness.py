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
    MAX_ARTIFACT_BYTES,
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
    record_transcript: bool = False  # opt in: store the full conversation as a `transcript.jsonl` artifact


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


_TRANSCRIPT_NAME = "transcript.jsonl"
_TEXT_FIELDS = ("text", "reasoning", "content")
_MIN_KEEP = 200


def _clip(value: str, keep: int) -> str:
    return value if len(value) <= keep else f"{value[:keep]}…[truncated {len(value) - keep} chars]"


def _clip_value(value, keep: int):
    if isinstance(value, str):
        return _clip(value, keep)
    if isinstance(value, dict):
        return {k: _clip_value(v, keep) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_value(v, keep) for v in value]
    return value


def _encode_transcript(records: list[dict], limit: int) -> tuple[bytes, bool]:
    """JSON Lines. If too big, shorten long strings (keep the head) with a shrinking cap until it fits."""
    def encode(rs): return ("\n".join(json.dumps(r, ensure_ascii=False) for r in rs) + "\n").encode()
    data = encode(records)
    if len(data) <= limit:
        return data, False
    keep = max((limit // max(len(records), 1)) * 2, _MIN_KEEP)
    while True:
        data = encode([_clip_value(r, keep) for r in records])
        if len(data) <= limit or keep <= 16:
            break
        keep //= 2
    # Still too big: drop whole records before the final one (the `end` record) so every line stays valid JSON.
    clipped = [_clip_value(r, keep) for r in records]
    head, last = clipped[:-1], clipped[-1:]
    while head and len(data) > limit:
        head.pop()
        data = encode(head + last)
    return (data if len(data) <= limit else b""), True


def _store_transcript(sink: RunSink, records: list[dict], artifacts: list[ArtifactRef]) -> None:
    """Best effort, like output collection: a failure is an event, never a failed run."""
    try:
        data, truncated = _encode_transcript(records, MAX_ARTIFACT_BYTES)
        artifacts.append(sink.upload(_TRANSCRIPT_NAME, "text/plain", data))
        sink.append("transcript.recorded", {"records": len(records), "bytes": len(data), "truncated": truncated})
    except ArtifactRejected as exc:
        sink.append("transcript.failed", {"code": exc.code})
    except SinkFailure:
        raise
    except Exception as exc:  # noqa: BLE001  # class name only
        sink.append("transcript.failed", {"code": type(exc).__name__})


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
    started = clock()
    records: list[dict] = []
    if run.record_transcript:
        records += [{"type": "system", "text": pinned.instructions}, {"type": "user", "text": run.instruction}]
    elapsed = lambda: round(clock() - started, 3)
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
            call_start = clock()
            response = adapter.converse(
                system=pinned.instructions, messages=list(messages), tools=specs,
                max_output_tokens=profile.max_output_tokens,
                timeout_seconds=min(float(profile.timeout_seconds), guard.remaining_seconds()),
            )
            latency_ms = int((clock() - call_start) * 1000)
            guard.record(response.input_tokens, response.output_tokens)
            in_tokens += response.input_tokens
            out_tokens += response.output_tokens
            ids = {k: v for k, v in (("provider", response.provider), ("response_id", response.response_id),
                                     ("model", response.model)) if v is not None}
            sink.append("model.response", {
                "turn": turn, "stop_reason": response.stop_reason, "tool_calls": len(response.tool_calls),
                "latency_ms": latency_ms, **ids})
            sink.append("usage.recorded", {
                "turn": turn, "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                "total_tokens": response.input_tokens + response.output_tokens,
                "cache_read_tokens": response.cache_read_tokens, "cache_write_tokens": response.cache_write_tokens,
                "reasoning_tokens": response.reasoning_tokens,
                **({"cost_usd": response.cost_usd} if response.cost_usd is not None else {})})
            if run.record_transcript:
                records.append({
                    "type": "assistant", "turn": turn, "t": elapsed(), "latency_ms": latency_ms,
                    "stop_reason": response.stop_reason, "text": response.text, "reasoning": response.reasoning,
                    "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls],
                    "usage": {"input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                              "cache_read_tokens": response.cache_read_tokens,
                              "cache_write_tokens": response.cache_write_tokens,
                              "reasoning_tokens": response.reasoning_tokens, "cost_usd": response.cost_usd},
                    "provider": response.provider, "response_id": response.response_id, "model": response.model})
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
                    tool_start = clock()
                    result = _invoke(tools, call)
                    duration_ms = int((clock() - tool_start) * 1000)
                    sink.append("tool.result", {
                        "turn": turn, "name": call.name, "result_bytes": len(result.content.encode()),
                        "is_error": result.is_error, "duration_ms": duration_ms, **server})
                    if run.record_transcript:
                        records.append({
                            "type": "tool", "turn": turn, "t": elapsed(), "duration_ms": duration_ms, "id": call.id,
                            "name": call.name, "arguments": call.arguments, "content": result.content,
                            "is_error": result.is_error})
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
    if run.record_transcript:
        records.append({"type": "end", "t": elapsed(), "status": status, "terminal_reason": reason})
        _store_transcript(sink, records, artifacts)

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
