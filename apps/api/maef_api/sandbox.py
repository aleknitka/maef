"""Sandbox seam: where a run's commands execute, isolated from the worker host.

`Sandbox.provision(profile, run_id)` returns a `Session`, one container for one run. The only
production adapter is `OciCliSandbox`, which shells out to the `podman` or `docker` CLI using
the subset both share (run/exec/rm/ps/info). It never uses a runtime API socket, and mounts
nothing into the container. `FakeSandbox` is the in-memory adapter for tests.

Image requirements: POSIX `sh`, `cat`, `mkdir` (used by put/get), and a `sleep` that accepts
`infinity` (GNU or busybox). Container output and file contents are never logged.
"""

import json
import os
import posixpath
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from . import egress_proxy
from .models import SandboxProfile, is_pinned_image

MAX_OUTPUT_BYTES = 64 * 1024
MAX_GET_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024  # longest line a spawned process may emit
WORKSPACE = "/workspace"
ALLOW_UNPINNED_ENV = "MAEF_ALLOW_UNPINNED_IMAGES"
RUNTIME_ENV = "MAEF_OCI_RUNTIME"
# Multi-arch manifest-list digest of docker.io/library/python:3.12-alpine (amd64 + arm64), pinned
# 2026-11-23. Re-pin with `podman image ls --digests` (the list digest, not one platform's).
EGRESS_PROXY_IMAGE = "docker.io/library/python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"
_PROVISION_TIMEOUT = 300.0
_CLI_TIMEOUT = 30.0
_CANCEL_POLL_SECONDS = 0.25


class SandboxError(Exception):
    """A sandbox operation failed. Messages carry codes, never container output."""


class SandboxUnavailable(SandboxError):
    """No acceptable sandbox: no runtime, runtime not rootless when required, image not allowed."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False


class Process(Protocol):
    """A long-lived process inside a session (an MCP server). Not thread-safe."""

    def write(self, data: bytes) -> None:
        """Raises SandboxError when the process is gone."""
        ...

    def readline(self, timeout: float) -> bytes | None:
        """The next line (b"" at end of output, at most MAX_LINE_BYTES + 1 bytes), or None on timeout."""
        ...

    def close(self) -> None:
        """Idempotent; never raises."""
        ...


Network = Literal["none", "allowlist", "open"]


@dataclass(frozen=True)
class EgressSpec:
    """What the egress proxy lets through for `network="allowlist"` (see egress_proxy.Policy)."""

    hosts: tuple[str, ...]
    ports: tuple[int, ...] = (443, 80)
    max_connections: int = 32
    idle_seconds: float = 60.0
    max_bytes: int = 256 * 1024 * 1024


class Session(Protocol):
    def exec(
        self, argv: list[str], *, stdin: bytes | None = None, timeout: float, cancel: threading.Event | None = None
    ) -> ExecResult:
        """Best effort: once `cancel` is set the exec is killed like a timeout and `cancelled` is True."""
        ...

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> Process:
        """Start a long-lived process with stdin/stdout pipes. `env` values never appear in argv."""
        ...

    def put(self, path: str, data: bytes) -> None: ...

    def get(self, path: str) -> bytes: ...

    def egress_summary(self) -> dict | None:
        """For `allowlist` sessions: {decisions, hosts, bytes} from the proxy's log (hosts only), else
        None. Call before teardown; never raises."""
        ...

    def teardown(self) -> None:
        """Idempotent; never raises. Also removes the proxy and network of an `allowlist` session."""
        ...


class Sandbox(Protocol):
    def provision(
        self, profile: SandboxProfile, run_id: str, *, network: Network = "none", egress: EgressSpec | None = None
    ) -> Session:
        """`network="open"` uses the runtime's default network, unfiltered; callers must have been
        granted it. `network="allowlist"` (needs `egress`) joins the container to a per-run internal
        network whose only way out is an enforced allowlist proxy."""
        ...

    def info(self) -> dict:
        """{runtime, version, rootless}"""
        ...


def workspace_path(path: str) -> str:
    """Normalised absolute path under /workspace, or ValueError. `..` is rejected outright."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path or ".." in path.split("/"):
        raise ValueError("path_outside_workspace")
    norm = posixpath.normpath(path)
    if not norm.startswith(WORKSPACE + "/"):
        raise ValueError("path_outside_workspace")
    return norm


def _cap(data: bytes | None) -> bytes:
    return (data or b"")[:MAX_OUTPUT_BYTES]


class OciCliSandbox:
    def __init__(self, runtime: str, binary: str, env: Mapping[str, str] | None = None):
        if runtime not in ("podman", "docker"):
            raise ValueError(f"Unsupported OCI runtime: {runtime}")
        self.runtime, self._bin = runtime, binary
        self._env = os.environ if env is None else env
        self._info: dict | None = None

    # -- helpers --
    def _run(
        self, args: list[str], *, timeout: float = _CLI_TIMEOUT, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._bin, *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            check=False,
        )

    def _out(self, args: list[str]) -> str | None:
        try:
            done = self._run(args)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode(errors="replace").strip() if done.returncode == 0 else None

    def usable(self) -> bool:
        """The runtime answers `info` (CLI works and, for docker, the daemon is reachable)."""
        return self._out(["info", "--format", "{{.}}"]) is not None

    def info(self) -> dict:
        if self._info is None:
            version = self._out(["--version"]) or ""
            m = re.search(r"(\d+\.\d+[\w.+-]*)", version)
            if self.runtime == "podman":
                rootless = self._out(["info", "--format", "{{.Host.Security.Rootless}}"]) == "true"
            else:
                rootless = "rootless" in (self._out(["info", "--format", "{{json .SecurityOptions}}"]) or "")
            self._info = {"runtime": self.runtime, "version": m.group(1) if m else "unknown", "rootless": rootless}
        return dict(self._info)

    # -- Sandbox --
    def provision(
        self, profile: SandboxProfile, run_id: str, *, network: Network = "none", egress: EgressSpec | None = None
    ) -> Session:
        if profile.require_rootless and not self.info()["rootless"]:
            raise SandboxUnavailable("runtime_not_rootless")  # fail closed, never downgrade
        if not is_pinned_image(profile.image) and self._env.get(ALLOW_UNPINNED_ENV) != "1":
            raise SandboxUnavailable("unpinned_image")
        if network == "allowlist" and (egress is None or not egress.hosts or not egress.ports):
            raise SandboxUnavailable("egress_not_configured")
        safe = re.sub(r"[^A-Za-z0-9]", "", run_id)[:12] or "run"
        suffix = secrets.token_hex(4)
        name = f"maef-{safe}-{suffix}"
        proxy = net = None
        net_args: list[str] = []
        if network == "none":
            net_args = ["--network=none"]
        elif network == "allowlist":
            net, proxy = f"maef-egress-{safe}-{suffix}", f"maef-proxy-{safe}-{suffix}"
            try:
                proxy_ip = self._start_egress(net, proxy, run_id, egress)
            except SandboxError:
                self._remove_egress(net, proxy)
                raise
            url = f"http://{proxy_ip}:{egress_proxy.PROXY_PORT}"
            net_args = [
                f"--network={net}",
                "--env",
                "NO_PROXY=",
                "--env",
                "no_proxy=",
                *(
                    x
                    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
                    for x in ("--env", f"{k}={url}")
                ),
            ]
        args = [
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            f"maef.run={run_id}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            *net_args,
            "--pids-limit",
            str(profile.pids_limit),
            "--memory",
            f"{profile.memory_mb}m",
            "--cpus",
            str(profile.cpus),
            "--user",
            profile.user,
            "--tmpfs",
            f"{WORKSPACE}:rw,size={profile.workspace_mb}m,mode=1777",
            "--tmpfs",
            "/tmp:rw,size=16m,mode=1777",
            "--entrypoint",
            "sleep",
            profile.image,
            "infinity",
        ]
        try:
            done = self._run(args, timeout=_PROVISION_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            self._rm(name)
            self._remove_egress(net, proxy)
            raise SandboxUnavailable("provision_failed") from None
        if done.returncode != 0:
            self._rm(name)
            self._remove_egress(net, proxy)
            raise SandboxUnavailable("provision_failed")
        return _OciSession(self, name, proxy=proxy, network=net)

    def _start_egress(self, net: str, proxy: str, run_id: str, spec: EgressSpec) -> str:
        """Returns the proxy's IP on the internal network. Internal network (no route out) plus a proxy that is on the runtime's default network
        (for DNS and the way out) and on the internal network. The proxy starts on the default network
        because an internal network's DNS does not forward (podman: the network has DNS disabled so the
        proxy keeps the host's resolvers; clients reach the proxy by IP); it accepts clients only from the internal
        network's subnet, so other containers on the default network cannot use it."""
        label = f"maef.run={run_id}"
        default = "podman" if self.runtime == "podman" else "bridge"
        try:
            if (
                self._run(
                    [
                        "network",
                        "create",
                        "--internal",
                        "--label",
                        label,
                        *(["--disable-dns"] if self.runtime == "podman" else []),
                        net,
                    ]
                ).returncode
                != 0
            ):
                raise SandboxUnavailable("egress_network_failed")
            fmt = (
                "{{range .Subnets}}{{.Subnet}} {{end}}"
                if self.runtime == "podman"
                else "{{range .IPAM.Config}}{{.Subnet}} {{end}}"
            )
            subnets = (self._out(["network", "inspect", "--format", fmt, net]) or "").split()
            if not subnets:
                raise SandboxUnavailable("egress_network_failed")
            config = json.dumps(
                {
                    "hosts": list(spec.hosts),
                    "ports": list(spec.ports),
                    "max_connections": spec.max_connections,
                    "idle_seconds": spec.idle_seconds,
                    "max_bytes": spec.max_bytes,
                    "client_cidrs": subnets,
                }
            )
            source = Path(egress_proxy.__file__).read_text(encoding="utf-8")
            done = self._run(
                [
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    proxy,
                    "--label",
                    label,
                    "--network",
                    default,
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit",
                    "128",
                    "--memory",
                    "128m",
                    "--cpus",
                    "0.5",
                    "--user",
                    "65534:65534",
                    "--tmpfs",
                    "/tmp:rw,size=8m,mode=1777",
                    "--env",
                    f"MAEF_EGRESS_CONFIG={config}",
                    "--entrypoint",
                    "python",
                    EGRESS_PROXY_IMAGE,
                    "-u",
                    "-c",
                    source,
                ],
                timeout=_PROVISION_TIMEOUT,
            )
            if done.returncode != 0:
                raise SandboxUnavailable("egress_proxy_failed")
            if self._run(["network", "connect", net, proxy]).returncode != 0:
                raise SandboxUnavailable("egress_proxy_failed")
            deadline = time.monotonic() + 30
            while '"event": "ready"' not in (self._logs(proxy) or ""):
                if time.monotonic() > deadline or self._out(["ps", "-q", "--filter", f"name=^{proxy}$"]) == "":
                    raise SandboxUnavailable("egress_proxy_failed")
                time.sleep(0.2)
            ip = self._out(
                ["inspect", "--format", f'{{{{(index .NetworkSettings.Networks "{net}").IPAddress}}}}', proxy]
            )
            if not ip or not re.fullmatch(r"[0-9a-fA-F:.]+", ip):
                raise SandboxUnavailable("egress_proxy_failed")
            return ip
        except (OSError, subprocess.SubprocessError):
            raise SandboxUnavailable("egress_proxy_failed") from None

    def _logs(self, name: str) -> str | None:
        try:
            done = self._run(["logs", name])
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode(errors="replace") if done.returncode == 0 else None

    def _remove_egress(self, net: str | None, proxy: str | None) -> None:
        if proxy:
            self._rm(proxy)
        if net:
            try:
                self._run(["network", "rm", "-f", net])
            except (OSError, subprocess.SubprocessError):
                pass

    def _rm(self, name: str) -> None:
        """Remove without a stop grace period. Every MAEF container's PID 1 (`sleep infinity`, the proxy) ignores
        SIGTERM and holds no state worth flushing, so podman's default 10 s wait would only delay teardown.
        `docker rm -f` already kills immediately."""
        try:
            self._run(["rm", "-f", "-t", "0", name] if self.runtime == "podman" else ["rm", "-f", name])
        except (OSError, subprocess.SubprocessError):
            pass

    def cleanup_orphans(self) -> int:
        """Remove every container labelled maef.run. Call at worker start; assumes this host's
        containers belong to this worker (two workers on one host would remove each other's)."""
        ids = (self._out(["ps", "-a", "-q", "--filter", "label=maef.run"]) or "").split()
        for cid in ids:
            self._rm(cid)
        nets = (self._out(["network", "ls", "-q", "--filter", "label=maef.run"]) or "").split()
        for net in nets:
            self._remove_egress(net, None)
        return len(ids)


class _OciSession:
    def __init__(self, sandbox: OciCliSandbox, name: str, *, proxy: str | None = None, network: str | None = None):
        self._sb, self.name, self._down = sandbox, name, False
        self._proxy, self._network = proxy, network
        self._summary: dict | None = None

    def egress_summary(self) -> dict | None:
        if self._proxy is None:
            return None
        if self._summary is None:
            text = self._sb._logs(self._proxy)
            if text is None:
                return None
            self._summary = egress_proxy.summarize_log(text)
        return self._summary

    def exec(
        self, argv: list[str], *, stdin: bytes | None = None, timeout: float, cancel: threading.Event | None = None
    ) -> ExecResult:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("invalid_argv")
        if cancel is not None:
            return self._exec_cancellable(argv, stdin, timeout, cancel)
        try:
            done = self._sb._run(
                ["exec", "-i", "-w", WORKSPACE, self.name, *argv], timeout=max(timeout, 0.1), stdin=stdin or b""
            )
        except subprocess.TimeoutExpired as exc:
            self._kill_all()
            out = exc.stdout if isinstance(exc.stdout, bytes) else None
            err = exc.stderr if isinstance(exc.stderr, bytes) else None
            return ExecResult(-1, _cap(out), _cap(err), True)
        except OSError:
            raise SandboxError("exec_failed") from None
        return ExecResult(done.returncode, _cap(done.stdout), _cap(done.stderr))

    def _exec_cancellable(
        self, argv: list[str], stdin: bytes | None, timeout: float, cancel: threading.Event
    ) -> ExecResult:
        """Like exec, but waits in short slices so `cancel` can interrupt it."""
        if cancel.is_set():
            return ExecResult(-1, b"", b"", cancelled=True)
        cmd = [self._sb._bin, "exec", "-i", "-w", WORKSPACE, self.name, *argv]
        try:
            popen = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise SandboxError("exec_failed") from None
        deadline = time.monotonic() + max(timeout, 0.1)
        data = stdin or b""
        while True:
            try:
                out, err = popen.communicate(input=data, timeout=_CANCEL_POLL_SECONDS)
                return ExecResult(popen.returncode, _cap(out), _cap(err))
            except subprocess.TimeoutExpired as exc:
                data = None  # communicate() resumes; stdin was already handed over
                timed_out, cancelled = time.monotonic() >= deadline, cancel.is_set()
                if not (timed_out or cancelled):
                    continue
                popen.kill()
                self._kill_all()
                try:
                    out, err = popen.communicate(timeout=5)
                except (subprocess.SubprocessError, OSError):
                    out, err = exc.stdout, exc.stderr
                return ExecResult(
                    -1,
                    _cap(out if isinstance(out, bytes) else None),
                    _cap(err if isinstance(err, bytes) else None),
                    timed_out=timed_out and not cancelled,
                    cancelled=cancelled,
                )
            except OSError:
                popen.kill()
                raise SandboxError("exec_failed") from None

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> Process:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("invalid_argv")
        # `-e NAME` (no value) makes the CLI read the value from its own environment, so secrets
        # never appear in argv or `ps` output.
        cmd = [self._sb._bin, "exec", "-i", "-w", WORKSPACE, *(x for k in env for x in ("-e", k)), self.name, *argv]
        try:
            popen = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={**os.environ, **env}
            )
        except OSError:
            raise SandboxError("spawn_failed") from None
        return _OciProcess(popen)

    def _kill_all(self) -> None:
        # Best effort: killing the CLI client leaves the exec'd process running in the container.
        # `kill -9 -1` reaches every process the user may signal except PID 1 (sleep), which is immune.
        try:
            self._sb._run(["exec", self.name, "sh", "-c", "kill -9 -1"], timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def put(self, path: str, data: bytes) -> None:
        path = workspace_path(path)
        try:
            done = self._sb._run(
                ["exec", "-i", self.name, "sh", "-c", 'mkdir -p "${1%/*}" && cat > "$1"', "_", path], stdin=data
            )
        except (OSError, subprocess.SubprocessError):
            raise SandboxError("put_failed") from None
        if done.returncode != 0:
            raise SandboxError("put_failed")

    def get(self, path: str) -> bytes:
        path = workspace_path(path)
        try:
            done = self._sb._run(["exec", self.name, "cat", path])
        except (OSError, subprocess.SubprocessError):
            raise SandboxError("get_failed") from None
        if done.returncode != 0:
            raise SandboxError("get_failed")
        if len(done.stdout) > MAX_GET_BYTES:
            raise SandboxError("file_too_large")
        return done.stdout

    def teardown(self) -> None:
        if not self._down:
            self._down = True
            self.egress_summary()  # last chance: the proxy's log goes with its container
            self._sb._rm(self.name)
            self._sb._remove_egress(self._network, self._proxy)


class _OciProcess:
    """Pipes of an `exec -i` client. A reader thread feeds a bounded queue so readline can time out
    and a chatty process is held back by the pipe instead of growing memory."""

    def __init__(self, popen: subprocess.Popen):
        self._p = popen
        self._lines: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._closed = threading.Event()
        threading.Thread(target=self._pump, name="maef-oci-reader", daemon=True).start()

    def _put(self, line: bytes) -> None:
        while not self._closed.is_set():
            try:
                self._lines.put(line, timeout=0.2)
                return
            except queue.Full:
                continue

    def _pump(self) -> None:
        try:
            while not self._closed.is_set():
                line = self._p.stdout.readline(MAX_LINE_BYTES + 1)
                self._put(line)
                if not line:
                    return
        except (OSError, ValueError):
            self._put(b"")

    def write(self, data: bytes) -> None:
        try:
            self._p.stdin.write(data)
            self._p.stdin.flush()
        except (OSError, ValueError):
            raise SandboxError("process_write_failed") from None

    def readline(self, timeout: float) -> bytes | None:
        try:
            return self._lines.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed.set()
        for stream in (self._p.stdin, self._p.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._p.terminate()
            self._p.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                self._p.kill()
            except OSError:
                pass


def detect_sandbox(
    preference: tuple[str, ...] = ("podman", "docker"), env: Mapping[str, str] = os.environ
) -> OciCliSandbox | None:
    """First available runtime in `preference`; MAEF_OCI_RUNTIME (podman|docker) forces one."""
    forced = env.get(RUNTIME_ENV)
    if forced:
        if forced not in ("podman", "docker"):
            return None
        preference = (forced,)
    for runtime in preference:
        binary = shutil.which(runtime)
        if binary:
            sandbox = OciCliSandbox(runtime, binary, env)
            if sandbox.usable():  # skips dead shims (e.g. Docker Desktop stubs on a WSL2 PATH)
                return sandbox
    return None


# --- test adapter ---


@dataclass
class FakeSession:
    handler: Callable[[list[str], bytes | None], ExecResult]
    files: dict[str, bytes] = field(default_factory=dict)
    execs: list[list[str]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    torn_down: bool = False
    # Builds the Process for spawn(); tests inject a fake MCP server (see mcp.FakeMcpServer).
    spawn_factory: Callable[[list[str], dict[str, str]], Process] | None = None
    spawned: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)
    # What egress_summary() returns (tests set it); only meaningful for network="allowlist".
    egress: dict | None = None

    def egress_summary(self) -> dict | None:
        return self.egress

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> Process:
        self.spawned.append((list(argv), dict(env)))
        if self.spawn_factory is None:
            raise SandboxError("spawn_failed")
        return self.spawn_factory(list(argv), dict(env))

    def exec(
        self, argv: list[str], *, stdin: bytes | None = None, timeout: float, cancel: threading.Event | None = None
    ) -> ExecResult:
        self.execs.append(list(argv))
        self.timeouts.append(timeout)
        if cancel is not None and cancel.is_set():
            return ExecResult(-1, b"", b"", cancelled=True)
        result = self.handler(argv, stdin)
        return ExecResult(
            result.exit_code, _cap(result.stdout), _cap(result.stderr), result.timed_out, result.cancelled
        )

    def put(self, path: str, data: bytes) -> None:
        self.files[workspace_path(path)] = data

    def get(self, path: str) -> bytes:
        try:
            return self.files[workspace_path(path)]
        except KeyError:
            raise SandboxError("get_failed") from None

    def teardown(self) -> None:
        self.torn_down = True


class FakeSandbox:
    def __init__(
        self,
        handler: Callable[[list[str], bytes | None], ExecResult] | None = None,
        *,
        rootless: bool = True,
        fail: str | None = None,
        allow_unpinned: bool = True,
        spawn_factory: Callable[[list[str], dict[str, str]], Process] | None = None,
    ):
        self._handler = handler or (lambda argv, stdin: ExecResult(0, b"", b""))
        self._rootless, self._fail = rootless, fail
        self._spawn_factory, self._allow_unpinned = spawn_factory, allow_unpinned
        self.sessions: list[FakeSession] = []
        # (profile, run_id, network) per provision call, in order; egress specs in `egress_specs`.
        self.provisioned: list[tuple[SandboxProfile, str, str]] = []
        self.egress_specs: list[EgressSpec | None] = []

    def provision(
        self, profile: SandboxProfile, run_id: str, *, network: Network = "none", egress: EgressSpec | None = None
    ) -> FakeSession:
        if self._fail:
            raise SandboxUnavailable(self._fail)
        if profile.require_rootless and not self._rootless:
            raise SandboxUnavailable("runtime_not_rootless")
        if not is_pinned_image(profile.image) and not self._allow_unpinned:
            raise SandboxUnavailable("unpinned_image")
        if network == "allowlist" and (egress is None or not egress.hosts):
            raise SandboxUnavailable("egress_not_configured")
        session = FakeSession(self._handler, spawn_factory=self._spawn_factory)
        self.egress_specs.append(egress)
        self.provisioned.append((profile, run_id, network))
        self.sessions.append(session)
        return session

    def info(self) -> dict:
        return {"runtime": "fake", "version": "0", "rootless": self._rootless}
