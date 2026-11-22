"""Worker: claims a lease, verifies the pinned definition, runs the harness, reports back.

The worker never touches the ledger; it only speaks WorkerChannel. Model credentials resolve
here, from the worker's own environment: a lease never carries secrets.
"""
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable

from .adapters.anthropic import AnthropicAdapter
from .adapters.base import ModelAdapter
from .adapters.bedrock import BedrockAdapter
from .adapters.openai_compatible import OpenAICompatibleAdapter
from .channel import HttpChannel, WorkerChannel
from .credentials import Credentials, Secret
from .definitions import compute_definition_digest
from .harness import PreparedRun, SinkFailure, execute
from .leases import ChannelError, FencingError, Lease, SequenceGap
from .models import ModelEndpointProfile
from .tools import BuiltinTools

log = logging.getLogger("maef.worker")

AdapterFactory = Callable[[ModelEndpointProfile], ModelAdapter]
SEND_ATTEMPTS = 3


class NoAdapterForDialect(Exception):
    pass


def default_adapter_factory(credentials: Credentials) -> AdapterFactory:
    def factory(profile: ModelEndpointProfile) -> ModelAdapter:
        if profile.api_dialect == "bedrock-converse":
            return BedrockAdapter(profile)
        if profile.api_dialect == "openai-compatible":
            return OpenAICompatibleAdapter(profile, credentials)
        if profile.api_dialect == "anthropic":
            return AnthropicAdapter(profile, credentials)
        raise NoAdapterForDialect(profile.api_dialect)
    return factory


class ChannelSink:
    """RunSink over a WorkerChannel. Numbers its own events (wseq from 1) so a retried send is
    idempotent. Any failure that retrying cannot fix becomes SinkFailure."""

    def __init__(self, channel: WorkerChannel, lease: Lease, abandoned: threading.Event | None = None,
                 sleep=time.sleep):
        self._channel, self._lease, self._abandoned, self._sleep = channel, lease, abandoned, sleep
        self._wseq = 0

    def _send(self, call: Callable[[], object]) -> None:
        for attempt in range(SEND_ATTEMPTS):
            if self._abandoned is not None and self._abandoned.is_set():
                raise SinkFailure("lease_lost")
            try:
                call()
                return
            except (FencingError, SequenceGap) as exc:
                raise SinkFailure(type(exc).__name__) from None
            except ChannelError as exc:
                if attempt == SEND_ATTEMPTS - 1:
                    raise SinkFailure(str(exc)) from None
                self._sleep(min(2 ** attempt, 4))

    def append(self, type: str, payload: dict | None = None, *, status: str | None = None) -> None:
        wseq = self._wseq + 1
        self._send(lambda: self._channel.report(self._lease, wseq, type, payload, status))
        self._wseq = wseq

    def complete(self, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._send(lambda: self._channel.finish(
            self._lease, tokens_used=tokens_used, status=status, terminal_reason=terminal_reason))


class _Heartbeat(threading.Thread):
    def __init__(self, channel: WorkerChannel, lease: Lease, interval: float):
        super().__init__(name=f"maef-heartbeat-{lease.run_id[:8]}", daemon=True)
        self._channel, self._lease, self._interval = channel, lease, interval
        self._stop_event = threading.Event()
        self.lost = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._channel.heartbeat(self._lease)
            except FencingError:
                self.lost.set()
                return
            except ChannelError:
                log.warning("heartbeat failed for run %s; will retry", self._lease.run_id)

    def stop(self) -> None:
        self._stop_event.set()
        self.join()


class Worker:
    def __init__(self, channel: WorkerChannel, adapter_factory: AdapterFactory, *, worker_id: str,
                 heartbeat_seconds: float = 15, clock=time.monotonic):
        self._channel, self._adapter_factory = channel, adapter_factory
        self.worker_id, self._heartbeat_seconds, self._clock = worker_id, heartbeat_seconds, clock

    def run_once(self) -> bool:
        """Claim and run at most one lease. Returns whether a lease was run."""
        lease = self._channel.claim(self.worker_id)
        if lease is None:
            return False
        heartbeat = _Heartbeat(self._channel, lease, self._heartbeat_seconds)
        heartbeat.start()
        try:
            self._run(lease, heartbeat.lost)
        except SinkFailure as exc:
            log.error("run %s abandoned: %s", lease.run_id, exc)
        finally:
            heartbeat.stop()
        return True

    def _run(self, lease: Lease, abandoned: threading.Event) -> None:
        task = lease.task
        pinned = task.pinned
        sink = ChannelSink(self._channel, lease, abandoned)

        def reject(reason: str, **detail) -> None:
            # Nothing ran, so nothing is charged and the whole reservation is released.
            sink.append("run.rejected", {"reason": reason, **detail}, status="running")
            sink.complete(tokens_used=0, status="failed", terminal_reason=reason)

        if compute_definition_digest(pinned.definition_bytes, pinned.files) != pinned.definition_digest:
            return reject("definition_mismatch")
        try:
            adapter = self._adapter_factory(pinned.profile)
            tools = BuiltinTools(pinned.agent.tools, pinned.files)
        except Exception as exc:  # noqa: BLE001  # class name only; messages may carry secrets
            return reject(f"setup_error:{type(exc).__name__}")
        run = PreparedRun(lease.run_id, pinned, task.budget, task.reserved_tokens, task.instruction, tools)
        execute(sink, run, adapter, self._clock)

    def serve(self, stop: threading.Event, poll_seconds: float = 2) -> None:
        """Run leases until `stop` is set; an in-flight run is finished first."""
        while not stop.is_set():
            try:
                ran = self.run_once()
            except Exception as exc:  # noqa: BLE001  # keep serving; the sweep reclaims anything lost
                log.warning("worker loop error: %s", type(exc).__name__)
                ran = False
            if not ran:
                stop.wait(poll_seconds)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = os.environ.get("MAEF_WORKER_TOKEN")
    if not token:
        log.error("MAEF_WORKER_TOKEN is required")
        return 2
    channel = HttpChannel(
        os.environ.get("MAEF_API_URL", "http://127.0.0.1:8000"), Secret(token),
        allow_private=os.environ.get("MAEF_API_ALLOW_PRIVATE") == "1")
    worker_id = os.environ.get("MAEF_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    worker = Worker(channel, default_adapter_factory(Credentials()), worker_id=worker_id)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker %s serving", worker_id)
    worker.serve(stop)
    log.info("worker %s stopped", worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
