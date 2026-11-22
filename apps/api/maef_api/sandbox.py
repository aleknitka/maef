"""Sandbox seam: where a run's commands execute, isolated from the worker host.

`Sandbox.provision(profile, run_id)` returns a `Session`, one container for one run. The only
production adapter is `OciCliSandbox`, which shells out to the `podman` or `docker` CLI using
the subset both share (run/exec/rm/ps/info). It never uses a runtime API socket, and mounts
nothing into the container. `FakeSandbox` is the in-memory adapter for tests.

Image requirements: POSIX `sh`, `cat`, `mkdir` (used by put/get), and a `sleep` that accepts
`infinity` (GNU or busybox). Container output and file contents are never logged.
"""
import os
import posixpath
import queue
import re
import secrets
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .models import SandboxProfile, is_pinned_image

MAX_OUTPUT_BYTES = 64 * 1024
MAX_GET_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024  # longest line a spawned process may emit
WORKSPACE = "/workspace"
ALLOW_UNPINNED_ENV = "MAEF_ALLOW_UNPINNED_IMAGES"
RUNTIME_ENV = "MAEF_OCI_RUNTIME"
_PROVISION_TIMEOUT = 300.0
_CLI_TIMEOUT = 30.0


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


Network = Literal["none", "open"]


class Session(Protocol):
    def exec(self, argv: list[str], *, stdin: bytes | None = None, timeout: float) -> ExecResult: ...

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> Process:
        """Start a long-lived process with stdin/stdout pipes. `env` values never appear in argv."""
        ...

    def put(self, path: str, data: bytes) -> None: ...

    def get(self, path: str) -> bytes: ...

    def teardown(self) -> None:
        """Idempotent; never raises."""
        ...


class Sandbox(Protocol):
    def provision(self, profile: SandboxProfile, run_id: str, *, network: Network = "none") -> Session:
        """`network="open"` uses the runtime's default network; callers must have been granted it."""
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
    def _run(self, args: list[str], *, timeout: float = _CLI_TIMEOUT, stdin: bytes | None = None
             ) -> subprocess.CompletedProcess:
        return subprocess.run([self._bin, *args], input=stdin, capture_output=True, timeout=timeout,
                              stdin=None if stdin is not None else subprocess.DEVNULL, check=False)

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
    def provision(self, profile: SandboxProfile, run_id: str, *, network: Network = "none") -> Session:
        if profile.require_rootless and not self.info()["rootless"]:
            raise SandboxUnavailable("runtime_not_rootless")  # fail closed, never downgrade
        if not is_pinned_image(profile.image) and self._env.get(ALLOW_UNPINNED_ENV) != "1":
            raise SandboxUnavailable("unpinned_image")
        safe = re.sub(r"[^A-Za-z0-9]", "", run_id)[:12] or "run"
        name = f"maef-{safe}-{secrets.token_hex(4)}"
        args = [
            "run", "-d", "--rm", "--name", name, "--label", f"maef.run={run_id}",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            *(["--network=none"] if network == "none" else []),
            "--pids-limit", str(profile.pids_limit), "--memory", f"{profile.memory_mb}m",
            "--cpus", str(profile.cpus), "--user", profile.user,
            "--tmpfs", f"{WORKSPACE}:rw,size={profile.workspace_mb}m,mode=1777",
            "--tmpfs", "/tmp:rw,size=16m,mode=1777",
            "--entrypoint", "sleep", profile.image, "infinity",
        ]
        try:
            done = self._run(args, timeout=_PROVISION_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            self._rm(name)
            raise SandboxUnavailable("provision_failed") from None
        if done.returncode != 0:
            self._rm(name)
            raise SandboxUnavailable("provision_failed")
        return _OciSession(self, name)

    def _rm(self, name: str) -> None:
        try:
            self._run(["rm", "-f", name])
        except (OSError, subprocess.SubprocessError):
            pass

    def cleanup_orphans(self) -> int:
        """Remove every container labelled maef.run. Call at worker start; assumes this host's
        containers belong to this worker (two workers on one host would remove each other's)."""
        ids = (self._out(["ps", "-a", "-q", "--filter", "label=maef.run"]) or "").split()
        for cid in ids:
            self._rm(cid)
        return len(ids)


class _OciSession:
    def __init__(self, sandbox: OciCliSandbox, name: str):
        self._sb, self.name, self._down = sandbox, name, False

    def exec(self, argv: list[str], *, stdin: bytes | None = None, timeout: float) -> ExecResult:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("invalid_argv")
        try:
            done = self._sb._run(["exec", "-i", "-w", WORKSPACE, self.name, *argv],
                                 timeout=max(timeout, 0.1), stdin=stdin or b"")
        except subprocess.TimeoutExpired as exc:
            self._kill_all()
            out = exc.stdout if isinstance(exc.stdout, bytes) else None
            err = exc.stderr if isinstance(exc.stderr, bytes) else None
            return ExecResult(-1, _cap(out), _cap(err), True)
        except OSError:
            raise SandboxError("exec_failed") from None
        return ExecResult(done.returncode, _cap(done.stdout), _cap(done.stderr))

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> Process:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("invalid_argv")
        # `-e NAME` (no value) makes the CLI read the value from its own environment, so secrets
        # never appear in argv or `ps` output.
        cmd = [self._sb._bin, "exec", "-i", "-w", WORKSPACE, *(x for k in env for x in ("-e", k)), self.name, *argv]
        try:
            popen = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env={**os.environ, **env})
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
                ["exec", "-i", self.name, "sh", "-c", 'mkdir -p "${1%/*}" && cat > "$1"', "_", path], stdin=data)
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
            self._sb._rm(self.name)


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


def detect_sandbox(preference: tuple[str, ...] = ("podman", "docker"),
                   env: Mapping[str, str] = os.environ) -> OciCliSandbox | None:
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

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> Process:
        self.spawned.append((list(argv), dict(env)))
        if self.spawn_factory is None:
            raise SandboxError("spawn_failed")
        return self.spawn_factory(list(argv), dict(env))

    def exec(self, argv: list[str], *, stdin: bytes | None = None, timeout: float) -> ExecResult:
        self.execs.append(list(argv))
        self.timeouts.append(timeout)
        result = self.handler(argv, stdin)
        return ExecResult(result.exit_code, _cap(result.stdout), _cap(result.stderr), result.timed_out)

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
    def __init__(self, handler: Callable[[list[str], bytes | None], ExecResult] | None = None,
                 *, rootless: bool = True, fail: str | None = None, allow_unpinned: bool = True,
                 spawn_factory: Callable[[list[str], dict[str, str]], Process] | None = None):
        self._handler = handler or (lambda argv, stdin: ExecResult(0, b"", b""))
        self._rootless, self._fail = rootless, fail
        self._spawn_factory, self._allow_unpinned = spawn_factory, allow_unpinned
        self.sessions: list[FakeSession] = []
        # (profile, run_id, network) per provision call, in order.
        self.provisioned: list[tuple[SandboxProfile, str, str]] = []

    def provision(self, profile: SandboxProfile, run_id: str, *, network: Network = "none") -> FakeSession:
        if self._fail:
            raise SandboxUnavailable(self._fail)
        if profile.require_rootless and not self._rootless:
            raise SandboxUnavailable("runtime_not_rootless")
        if not is_pinned_image(profile.image) and not self._allow_unpinned:
            raise SandboxUnavailable("unpinned_image")
        session = FakeSession(self._handler, spawn_factory=self._spawn_factory)
        self.provisioned.append((profile, run_id, network))
        self.sessions.append(session)
        return session

    def info(self) -> dict:
        return {"runtime": "fake", "version": "0", "rootless": self._rootless}
