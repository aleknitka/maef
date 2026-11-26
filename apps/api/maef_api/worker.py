"""Worker: claims a lease, verifies the pinned definition, runs the harness, reports back.

The worker never touches the ledger; it only speaks WorkerChannel. Model credentials resolve
here, from the worker's own environment: a lease never carries secrets.
"""

import hashlib
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import yaml
from pydantic import ValidationError

from .adapters.anthropic import AnthropicAdapter
from .adapters.base import ModelAdapter
from .adapters.bedrock import BedrockAdapter
from .adapters.decision import (
    DecisionAdapter,
    DecisionsApiAdapter,
    WorkersAiDecisionAdapter,
)
from .adapters.openai_compatible import OpenAICompatibleAdapter
from .artifacts import ArtifactRef
from .channel import HttpChannel, WorkerChannel
from .credentials import CredentialError, Credentials, Secret
from .decisions import execute_decision
from .definitions import compute_definition_digest, mcp_ref_problem
from .harness import PreparedRun, SinkFailure, execute
from .leases import ChannelError, FencingError, Lease, SequenceGap
from .mcp import CompositeToolSet, McpError, McpToolSet, sandbox_profile_for
from .models import McpServerManifest, ModelEndpointProfile, is_pinned_image
from .outputs import collect_outputs
from .sandbox import EgressSpec, Sandbox, SandboxError, Session, detect_sandbox
from .tools import SANDBOXED_TOOLS, BuiltinTools

log = logging.getLogger("maef.worker")

AdapterFactory = Callable[[ModelEndpointProfile], ModelAdapter]
DecisionFactory = Callable[[ModelEndpointProfile], DecisionAdapter]
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


def default_decision_factory(credentials: Credentials) -> DecisionFactory:
    def factory(profile: ModelEndpointProfile) -> DecisionAdapter:
        if profile.api_dialect == "decisions-api":
            return DecisionsApiAdapter(profile, credentials)
        if profile.api_dialect == "workers-ai-decision":
            return WorkersAiDecisionAdapter(profile, credentials)
        raise NoAdapterForDialect(profile.api_dialect)

    return factory


class ChannelSink:
    """RunSink over a WorkerChannel. Numbers its own events (wseq from 1) so a retried send is
    idempotent. Any failure that retrying cannot fix becomes SinkFailure."""

    def __init__(
        self,
        channel: WorkerChannel,
        lease: Lease,
        abandoned: threading.Event | None = None,
        sleep=time.sleep,
        cancel: threading.Event | None = None,
    ):
        self._channel, self._lease, self._abandoned, self._sleep = channel, lease, abandoned, sleep
        self._cancel = cancel
        self._wseq = 0

    def _send(self, call: Callable[[], object]) -> object:
        for attempt in range(SEND_ATTEMPTS):
            if self._abandoned is not None and self._abandoned.is_set():
                raise SinkFailure("lease_lost")
            try:
                return call()
            except (FencingError, SequenceGap) as exc:
                raise SinkFailure(type(exc).__name__) from None
            except ChannelError as exc:
                if attempt == SEND_ATTEMPTS - 1:
                    raise SinkFailure(str(exc)) from None
                self._sleep(min(2**attempt, 4))

    def append(self, type: str, payload: dict | None = None, *, status: str | None = None) -> None:
        wseq = self._wseq + 1
        cancel_requested = self._send(lambda: self._channel.report(self._lease, wseq, type, payload, status))
        self._wseq = wseq
        if cancel_requested and self._cancel is not None:
            self._cancel.set()

    def upload(self, name: str, mime: str, data: bytes) -> ArtifactRef:
        return self._send(lambda: self._channel.upload(self._lease, name=name, mime=mime, data=data))

    def complete(self, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._send(
            lambda: self._channel.finish(
                self._lease, tokens_used=tokens_used, status=status, terminal_reason=terminal_reason
            )
        )


class _TeardownSink:
    """Wraps a sink so MCP servers and the sandbox session are torn down, with `mcp.stopped` and
    `sandbox.torn_down` recorded, before the terminal event (nothing can be appended once the run
    is terminal)."""

    def __init__(self, inner: ChannelSink, steps: list[Callable[[ChannelSink], None]]):
        self._inner, self._steps, self._done = inner, steps, False

    def append(self, type: str, payload: dict | None = None, *, status: str | None = None) -> None:
        self._inner.append(type, payload, status=status)

    def upload(self, name: str, mime: str, data: bytes) -> ArtifactRef:
        return self._inner.upload(name, mime, data)

    def teardown(self) -> None:
        if not self._done:
            self._done = True
            for step in self._steps:
                step(self._inner)

    def complete(self, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self.teardown()
        self._inner.complete(tokens_used=tokens_used, status=status, terminal_reason=terminal_reason)


class _StartupFailure(Exception):
    def __init__(self, reason: str, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


@dataclass
class _McpHandle:
    manifest: McpServerManifest
    session: Session
    tools: McpToolSet | None = None
    egress: dict | None = None  # proxy decision summary, taken once before teardown
    stopped: bool = False

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.tools is not None:
            self.tools.close()
        if self.manifest.network == "allowlist":
            try:
                self.egress = self.session.egress_summary()
            except Exception:  # noqa: BLE001  # the summary is best effort; teardown must still happen
                self.egress = None
        self.session.teardown()


def _report_egress(sink, handle: _McpHandle) -> None:
    if handle.manifest.network == "allowlist" and handle.egress is not None:
        sink.append("egress.summary", {"server": handle.manifest.id, **handle.egress})


class _Heartbeat(threading.Thread):
    def __init__(self, channel: WorkerChannel, lease: Lease, interval: float):
        super().__init__(name=f"maef-heartbeat-{lease.run_id[:8]}", daemon=True)
        self._channel, self._lease, self._interval = channel, lease, interval
        self._stop_event = threading.Event()
        self.lost = threading.Event()
        self.cancelled = threading.Event()  # set once the control plane reports a cancel request

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                if self._channel.heartbeat(self._lease):
                    self.cancelled.set()
            except FencingError:
                self.lost.set()
                return
            except ChannelError:
                log.warning("heartbeat failed for run %s; will retry", self._lease.run_id)

    def stop(self) -> None:
        self._stop_event.set()
        self.join()


class Worker:
    def __init__(
        self,
        channel: WorkerChannel,
        adapter_factory: AdapterFactory,
        *,
        worker_id: str,
        heartbeat_seconds: float = 15,
        clock=time.monotonic,
        sandbox: Sandbox | None = None,
        credentials: Credentials | None = None,
        decision_factory: DecisionFactory | None = None,
    ):
        self._channel, self._adapter_factory = channel, adapter_factory
        self.worker_id, self._heartbeat_seconds, self._clock = worker_id, heartbeat_seconds, clock
        self._sandbox = sandbox
        self._credentials = credentials or Credentials()
        self._decision_factory = decision_factory or default_decision_factory(self._credentials)

    def run_once(self) -> bool:
        """Claim and run at most one lease. Returns whether a lease was run."""
        lease = self._channel.claim(self.worker_id)
        if lease is None:
            return False
        heartbeat = _Heartbeat(self._channel, lease, self._heartbeat_seconds)
        heartbeat.start()
        try:
            self._run(lease, heartbeat.lost, heartbeat.cancelled)
        except SinkFailure as exc:
            log.error("run %s abandoned: %s", lease.run_id, exc)
        finally:
            heartbeat.stop()
        return True

    def _run_decision(self, lease: Lease, abandoned: threading.Event, cancel: threading.Event) -> None:
        task = lease.task
        d = task.decision
        sink = ChannelSink(self._channel, lease, abandoned, cancel=cancel)

        def reject(reason: str) -> None:
            sink.append("run.rejected", {"reason": reason}, status="running")
            sink.complete(tokens_used=0, status="failed", terminal_reason=reason)

        try:  # the profile the worker runs is exactly the pinned bytes
            consistent = (
                hashlib.sha256(d.profile_bytes).hexdigest() == d.profile_digest
                and ModelEndpointProfile.model_validate(yaml.safe_load(d.profile_bytes)) == d.profile
            )
        except (yaml.YAMLError, ValidationError):
            consistent = False
        if not consistent:
            return reject("definition_mismatch")
        try:
            adapter = self._decision_factory(d.profile)
        except Exception as exc:  # noqa: BLE001  # class name only
            return reject(f"setup_error:{type(exc).__name__}")
        execute_decision(sink, lease.run_id, task, adapter, cancel.is_set)

    def _run(self, lease: Lease, abandoned: threading.Event, cancel: threading.Event) -> None:
        task = lease.task
        if task.kind == "decision":
            return self._run_decision(lease, abandoned, cancel)
        pinned = task.pinned
        sink = ChannelSink(self._channel, lease, abandoned, cancel=cancel)

        def reject(reason: str, **detail) -> None:
            # Nothing ran, so nothing is charged and the whole reservation is released.
            sink.append("run.rejected", {"reason": reason, **detail}, status="running")
            sink.complete(tokens_used=0, status="failed", terminal_reason=reason)

        if compute_definition_digest(
            pinned.definition_bytes, pinned.files, pinned.mcp_bytes
        ) != pinned.definition_digest or not self._mcp_consistent(pinned):
            return reject("definition_mismatch")
        if mcp_ref_problem(pinned.agent, pinned.mcp_servers) == "network_not_permitted":
            return reject("network_not_permitted")
        needs_sandbox = any(t in SANDBOXED_TOOLS for t in pinned.agent.tools)
        if needs_sandbox and (self._sandbox is None or pinned.sandbox is None):
            return reject("sandbox_unavailable")
        deadline = self._clock() + task.budget.max_duration_seconds
        session = None
        mcp: list[_McpHandle] = []
        try:
            try:
                adapter = self._adapter_factory(pinned.profile)
                if needs_sandbox:
                    session = self._sandbox.provision(pinned.sandbox, lease.run_id)
                tools = BuiltinTools(
                    pinned.agent.tools,
                    pinned.files,
                    session=session,
                    exec_timeout=lambda: deadline - self._clock(),
                    cancel=cancel,
                )
            except SandboxError:
                return reject("sandbox_unavailable")
            except Exception as exc:  # noqa: BLE001  # class name only; messages may carry secrets
                return reject(f"setup_error:{type(exc).__name__}")
            try:
                mcp_sets = self._start_mcp(lease, pinned, mcp, lambda: deadline - self._clock())
                if mcp_sets:
                    tools = CompositeToolSet([tools, *mcp_sets])
            except _StartupFailure as failure:
                return reject(failure.reason, **failure.detail)
            network_open = pinned.agent.permissions.network_open
            if session is not None:
                sink.append(
                    "sandbox.provisioned",
                    {
                        **self._sandbox.info(),
                        "profile": pinned.sandbox.id,
                        "network": "none",
                        "network_open": network_open,
                        "unpinned_image_allowed": not is_pinned_image(pinned.sandbox.image),
                    },
                )
            for handle in mcp:
                manifest = handle.manifest
                sink.append(
                    "sandbox.provisioned",
                    {
                        **self._sandbox.info(),
                        "profile": manifest.id,
                        "server": manifest.id,
                        "network": manifest.network,
                        "network_open": network_open,
                        "unpinned_image_allowed": not is_pinned_image(manifest.image),
                    },
                )
                sink.append(
                    "mcp.started",
                    {
                        "server": manifest.id,
                        "version": manifest.version,
                        "tool_count": len(handle.tools.specs()),
                        "network": manifest.network,
                    },
                )
                if manifest.network == "allowlist":
                    sink.append(
                        "egress.proxy_started",
                        {
                            "server": manifest.id,
                            "allowed_hosts": len(manifest.allowed_hosts),
                            "any_public_host": "*" in manifest.allowed_hosts,
                            "ports": manifest.allowed_ports,
                        },
                    )
                if handle.tools.truncated:
                    sink.append(
                        "mcp.tools_truncated",
                        {"server": manifest.id, "listed": handle.tools.listed, "max_tools": manifest.max_tools},
                    )
            steps: list[Callable[[ChannelSink], None]] = []
            for handle in mcp:
                steps.append(
                    lambda s, h=handle: (
                        h.stop(),
                        s.append("mcp.stopped", {"server": h.manifest.id}),
                        _report_egress(s, h),
                    )
                )
            if session is not None:
                steps.append(
                    lambda s: (session.teardown(), s.append("sandbox.torn_down", {"profile": pinned.sandbox.id}))
                )
            run_sink = _TeardownSink(sink, steps) if steps else sink
            outputs = None if session is None else (lambda: collect_outputs(session))
            run = PreparedRun(
                lease.run_id,
                pinned,
                task.budget,
                task.reserved_tokens,
                task.instruction,
                tools,
                outputs,
                cancel.is_set,
                record_transcript=os.environ.get("MAEF_RECORD_TRANSCRIPT", "").lower() in ("1", "true", "yes"),
            )
            execute(run_sink, run, adapter, self._clock)
        finally:
            for handle in mcp:
                handle.stop()  # idempotent; covers abandoned runs, startup failures and exceptions
            if session is not None:
                session.teardown()

    @staticmethod
    def _mcp_consistent(pinned) -> bool:
        """The manifests the worker will run are exactly the pinned bytes, referenced by the agent."""
        try:
            parsed = [
                McpServerManifest.model_validate(yaml.safe_load(pinned.mcp_bytes[m.id].decode("utf-8")))
                for m in pinned.mcp_servers
            ]
        except (KeyError, UnicodeDecodeError, yaml.YAMLError, ValidationError):
            return False
        return (
            parsed == pinned.mcp_servers
            and set(pinned.mcp_bytes) == {m.id for m in pinned.mcp_servers}
            and mcp_ref_problem(pinned.agent, pinned.mcp_servers) in (None, "network_not_permitted")
        )

    def _start_mcp(
        self, lease: Lease, pinned, handles: list[_McpHandle], remaining: Callable[[], float]
    ) -> list[McpToolSet]:
        """Resolve secrets, then provision, spawn and initialise each server. Handles are appended as
        soon as a container exists so the caller can tear down a partial start."""
        if not pinned.mcp_servers:
            return []
        resolved: dict[str, dict[str, str]] = {}
        for manifest in pinned.mcp_servers:  # before any container exists or any model call
            env = dict(manifest.env)
            for name, ref in manifest.secrets.items():
                try:
                    secret = self._credentials.resolve(ref)
                except CredentialError:
                    raise _StartupFailure(f"credential_missing:{name}") from None
                if secret is None:
                    raise _StartupFailure(f"credential_missing:{name}")
                env[name] = secret.reveal()
            resolved[manifest.id] = env
        sets: list[McpToolSet] = []
        for manifest in pinned.mcp_servers:
            unavailable = f"mcp_unavailable:{manifest.id}"
            if self._sandbox is None:
                raise _StartupFailure(unavailable, code="sandbox_unavailable")
            try:
                session = self._sandbox.provision(
                    sandbox_profile_for(manifest, pinned.sandbox),
                    f"{lease.run_id}-{manifest.id}",
                    network=manifest.network,
                    egress=EgressSpec(tuple(manifest.allowed_hosts), tuple(manifest.allowed_ports))
                    if manifest.network == "allowlist"
                    else None,
                )
            except SandboxError as exc:
                raise _StartupFailure(unavailable, code=str(exc)) from None
            handle = _McpHandle(manifest, session)
            handles.append(handle)
            try:
                process = session.spawn(manifest.command, env=resolved[manifest.id])
                handle.tools = McpToolSet(manifest.id, process, max_tools=manifest.max_tools, timeout=remaining)
                handle.tools.start()
            except Exception as exc:  # noqa: BLE001  # codes and class names only; messages may echo secrets
                code = str(exc) if isinstance(exc, SandboxError | McpError) else type(exc).__name__
                raise _StartupFailure(unavailable, code=code) from None
            sets.append(handle.tools)
        return sets

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
    token = os.environ.get("MAEF_WORKER_CREDENTIAL")
    if not token:
        log.error("MAEF_WORKER_CREDENTIAL is required (create one with `maef admin worker create --id ID`)")
        return 2
    channel = HttpChannel(
        os.environ.get("MAEF_API_URL", "http://127.0.0.1:8000"),
        Secret(token),
        allow_private=os.environ.get("MAEF_API_ALLOW_PRIVATE") == "1",
    )
    worker_id = os.environ.get("MAEF_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    sandbox = detect_sandbox()
    if sandbox is None:
        log.warning("no OCI runtime (podman or docker) found; runs needing a sandbox will fail")
    else:
        removed = sandbox.cleanup_orphans()
        log.info("sandbox runtime %s; removed %d orphaned containers", sandbox.info(), removed)
    worker = Worker(channel, default_adapter_factory(Credentials()), worker_id=worker_id, sandbox=sandbox)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker %s serving", worker_id)
    worker.serve(stop)
    log.info("worker %s stopped", worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
