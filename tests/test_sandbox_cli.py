"""OCI CLI adapter tests against fake podman/docker scripts. No container runtime is ever run.

Each fake is a POSIX sh script that appends its argv to <tmp>/<name>.calls (one line per call, each
argument NUL-terminated, so argument boundaries survive) and answers the few subcommands the adapter
uses through a shell `case`. PATH is pointed at a private bin dir, so detection only sees the fakes.
"""

import os
import time

import pytest

from maef_api.models import SandboxProfile
from maef_api.sandbox import (
    MAX_OUTPUT_BYTES,
    RUNTIME_ENV,
    OciCliSandbox,
    SandboxUnavailable,
    detect_sandbox,
)

ORIG_PATH = os.environ.get("PATH", "/usr/bin:/bin")
TAG_IMAGE = "docker.io/library/busybox:1.37"
PINNED_IMAGE = "docker.io/library/busybox@sha256:" + "a" * 64
RUN_ID = "run-abc"


def profile(image=PINNED_IMAGE, **overrides):
    base = {
        "schema_version": "maef.dev/v1alpha1",
        "id": "restricted-local",
        "image": image,
        "cpus": 1,
        "memory_mb": 64,
        "pids_limit": 32,
        "workspace_mb": 8,
    }
    return SandboxProfile(**{**base, **overrides})


@pytest.fixture(autouse=True)
def bindir(tmp_path, monkeypatch):
    """PATH holds only the fakes, so a real runtime on this machine is never found."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


def make_cli(tmp_path, name, cases=""):
    log = tmp_path / f"{name}.calls"
    path = tmp_path / "bin" / name
    path.write_text(
        "#!/bin/sh\n"
        f"PATH='{ORIG_PATH}'\n"
        "{ printf '%s\\0' \"$@\"; printf '\\n'; } >> '" + str(log) + "'\n" + cases + "\nexit 0\n"
    )
    path.chmod(0o755)
    return path


def calls(tmp_path, name):
    log = tmp_path / f"{name}.calls"
    if not log.exists():
        return []
    return [[a.decode() for a in line.split(b"\0")[:-1]] for line in log.read_bytes().split(b"\n") if line]


def podman_cases(rootless="true", info_ok=True):
    info = "echo true" if info_ok else "exit 1"
    return (
        'case "$*" in\n'
        '  --version*) echo "podman version 5.2.1";;\n'
        f"  *Rootless*) echo {rootless};;\n"
        f"  info*) {info};;\n"
        "esac"
    )


def docker_cases(rootless=True, info_ok=True):
    opts = '["name=seccomp,profile=builtin","name=rootless"]' if rootless else '["name=seccomp,profile=builtin"]'
    info = "echo ok" if info_ok else "exit 1"
    return (
        'case "$*" in\n'
        '  --version*) echo "Docker version 27.1.1, build abc";;\n'
        f"  *SecurityOptions*) echo '{opts}';;\n"
        f"  info*) {info};;\n"
        "esac"
    )


CASES = {"podman": podman_cases(), "docker": docker_cases()}


def sandbox(tmp_path, runtime, cases="", env=None):
    make_cli(tmp_path, runtime, CASES[runtime] + "\n" + cases)
    return OciCliSandbox(runtime, str(tmp_path / "bin" / runtime), env=env or {})


def runs(tmp_path, runtime):
    return [c for c in calls(tmp_path, runtime) if c[0] == "run"]


def opt(argv, flag):
    return argv[argv.index(flag) + 1]


def opts(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


# --- detection ---


def test_forced_runtime_wins_over_podman(tmp_path):
    make_cli(tmp_path, "podman", podman_cases())
    make_cli(tmp_path, "docker", docker_cases())
    sb = detect_sandbox(env={RUNTIME_ENV: "docker"})
    assert sb is not None and sb.runtime == "docker"


def test_default_preference_picks_podman(tmp_path):
    make_cli(tmp_path, "podman", podman_cases())
    make_cli(tmp_path, "docker", docker_cases())
    assert detect_sandbox(env={}).runtime == "podman"


def test_docker_alone_is_picked(tmp_path):
    make_cli(tmp_path, "docker", docker_cases())
    assert detect_sandbox(env={}).runtime == "docker"


def test_binary_whose_info_fails_is_skipped(tmp_path):
    # Shape of the Docker Desktop shim on a WSL2 PATH: present, but `info` exits non-zero.
    make_cli(tmp_path, "podman", podman_cases(info_ok=False))
    make_cli(tmp_path, "docker", docker_cases())
    assert detect_sandbox(env={}).runtime == "docker"


def test_no_runtime_returns_none(tmp_path):
    assert detect_sandbox(env={}) is None


def test_unsupported_forced_runtime_returns_none_without_fallback(tmp_path):
    make_cli(tmp_path, "podman", podman_cases())
    assert detect_sandbox(env={RUNTIME_ENV: "nerdctl"}) is None


# --- rootless detection and enforcement ---


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
def test_podman_rootless_from_info_format(tmp_path, value, expected):
    make_cli(tmp_path, "podman", podman_cases(rootless=value))
    sb = OciCliSandbox("podman", str(tmp_path / "bin" / "podman"), env={})
    assert sb.info() == {"runtime": "podman", "version": "5.2.1", "rootless": expected}
    assert ["info", "--format", "{{.Host.Security.Rootless}}"] in calls(tmp_path, "podman")


@pytest.mark.parametrize("rootless", [True, False])
def test_docker_rootless_from_security_options(tmp_path, rootless):
    make_cli(tmp_path, "docker", docker_cases(rootless=rootless))
    sb = OciCliSandbox("docker", str(tmp_path / "bin" / "docker"), env={})
    assert sb.info() == {"runtime": "docker", "version": "27.1.1", "rootless": rootless}


@pytest.mark.parametrize(
    "runtime, cases",
    [
        ("podman", podman_cases(rootless="false")),
        ("docker", docker_cases(rootless=False)),
    ],
)
def test_rootful_runtime_refused_at_provision(tmp_path, runtime, cases):
    make_cli(tmp_path, runtime, cases)
    sb = OciCliSandbox(runtime, str(tmp_path / "bin" / runtime), env={})
    with pytest.raises(SandboxUnavailable, match="runtime_not_rootless"):
        sb.provision(profile(), RUN_ID)
    assert runs(tmp_path, runtime) == []


def test_rootful_runtime_allowed_when_not_required(tmp_path):
    make_cli(tmp_path, "podman", podman_cases(rootless="false"))
    sb = OciCliSandbox("podman", str(tmp_path / "bin" / "podman"), env={})
    sb.provision(profile(require_rootless=False), RUN_ID)
    assert len(runs(tmp_path, "podman")) == 1


# --- run command line ---


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_run_carries_full_hardening(tmp_path, runtime):
    sandbox(tmp_path, runtime).provision(profile(), RUN_ID)
    (run,) = runs(tmp_path, runtime)
    for flag in ("-d", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--network=none"):
        assert flag in run
    assert opt(run, "--pids-limit") == "32"
    assert opt(run, "--memory") == "64m"
    assert opt(run, "--cpus") == "1.0"
    assert opt(run, "--user") == "65534:65534"
    assert opt(run, "--label") == f"maef.run={RUN_ID}"
    assert sorted(opts(run, "--tmpfs")) == ["/tmp:rw,size=16m,mode=1777", "/workspace:rw,size=8m,mode=1777"]
    assert run[-4:] == ["--entrypoint", "sleep", PINNED_IMAGE, "infinity"]


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_run_mounts_nothing_and_never_touches_a_socket(tmp_path, runtime):
    sandbox(tmp_path, runtime).provision(profile(), RUN_ID)
    (run,) = runs(tmp_path, runtime)
    assert not any(a in ("-v", "--volume", "--mount") or a.startswith(("--volume=", "--mount=")) for a in run)
    assert not any("sock" in a for a in run)


# --- image pinning ---


def test_tag_only_image_refused_by_default(tmp_path):
    sb = sandbox(tmp_path, "podman")
    with pytest.raises(SandboxUnavailable, match="unpinned_image"):
        sb.provision(profile(image=TAG_IMAGE), RUN_ID)
    assert runs(tmp_path, "podman") == []


@pytest.mark.parametrize("value", ["0", "true", ""])
def test_unpinned_opt_in_must_be_exactly_one(tmp_path, value):
    sb = sandbox(tmp_path, "podman", env={"MAEF_ALLOW_UNPINNED_IMAGES": value})
    with pytest.raises(SandboxUnavailable, match="unpinned_image"):
        sb.provision(profile(image=TAG_IMAGE), RUN_ID)


def test_tag_only_image_allowed_with_opt_in(tmp_path):
    sb = sandbox(tmp_path, "podman", env={"MAEF_ALLOW_UNPINNED_IMAGES": "1"})
    sb.provision(profile(image=TAG_IMAGE), RUN_ID)
    (run,) = runs(tmp_path, "podman")
    assert run[-2] == TAG_IMAGE


def test_digest_pinned_image_accepted_without_opt_in(tmp_path):
    sb = sandbox(tmp_path, "podman")
    session = sb.provision(profile(image=PINNED_IMAGE), RUN_ID)
    assert session.name.startswith("maef-")
    assert len(runs(tmp_path, "podman")) == 1


# --- exec, output caps, timeouts ---


def test_exec_passes_argv_items_separately(tmp_path):
    session = sandbox(tmp_path, "podman").provision(profile(), RUN_ID)
    session.exec(["printf", "%s", "a; rm -rf /"], timeout=5)
    assert calls(tmp_path, "podman")[-1] == [
        "exec",
        "-i",
        "-w",
        "/workspace",
        session.name,
        "printf",
        "%s",
        "a; rm -rf /",
    ]


def test_exec_output_truncated_to_cap(tmp_path):
    big = 'case "$1" in exec) head -c 100000 /dev/zero; head -c 100000 /dev/zero >&2 ;; esac'
    session = sandbox(tmp_path, "podman", big).provision(profile(), RUN_ID)
    r = session.exec(["anything"], timeout=5)
    assert (r.timed_out, r.truncated) == (False, True)  # the command is stopped once a stream passes the cap
    assert len(r.stdout) == MAX_OUTPUT_BYTES == 64 * 1024


def test_hanging_exec_times_out(tmp_path):
    hang = 'case "$*" in *"sleep 30"*) exec sleep 30 ;; esac'
    session = sandbox(tmp_path, "podman", hang).provision(profile(), RUN_ID)
    started = time.monotonic()
    r = session.exec(["sleep", "30"], timeout=0.5)
    assert time.monotonic() - started < 10
    assert (r.timed_out, r.exit_code) == (True, -1)
    assert ["exec", session.name, "sh", "-c", "kill -9 -1"] in calls(tmp_path, "podman")


# --- file transfer confinement ---


def test_put_and_get_reject_paths_outside_workspace_before_any_cli_call(tmp_path):
    session = sandbox(tmp_path, "podman").provision(profile(), RUN_ID)
    before = calls(tmp_path, "podman")
    for bad in ("/etc/passwd", "../etc/passwd", "/workspace/../etc/passwd", "/workspaceX/a"):
        with pytest.raises(ValueError):
            session.put(bad, b"x")
        with pytest.raises(ValueError):
            session.get(bad)
    assert calls(tmp_path, "podman") == before


def test_put_and_get_use_workspace_paths(tmp_path):
    session = sandbox(tmp_path, "podman").provision(profile(), RUN_ID)
    session.put("/workspace/sub/a.txt", b"data")
    put = calls(tmp_path, "podman")[-1]
    assert put[:4] == ["exec", "-i", session.name, "sh"] and put[-2:] == ["_", "/workspace/sub/a.txt"]
    assert session.get("/workspace/sub/a.txt") == b""
    assert calls(tmp_path, "podman")[-1] == ["exec", session.name, "cat", "/workspace/sub/a.txt"]


# --- teardown and orphan sweep ---


def test_teardown_removes_container_once(tmp_path):
    session = sandbox(tmp_path, "podman").provision(profile(), RUN_ID)
    session.teardown()
    session.teardown()
    assert [c for c in calls(tmp_path, "podman") if c[:2] == ["rm", "-f"]] == [["rm", "-f", "-t", "0", session.name]]


def test_teardown_never_raises_when_removal_fails(tmp_path):
    session = sandbox(tmp_path, "podman", 'case "$1" in rm) exit 1 ;; esac').provision(profile(), RUN_ID)
    session.teardown()
    session.teardown()  # second call is a no-op: the session is already marked down
    assert [c for c in calls(tmp_path, "podman") if c[:2] == ["rm", "-f"]] == [["rm", "-f", "-t", "0", session.name]]


def test_cleanup_orphans_removes_each_labelled_container(tmp_path):
    ps = 'case "$1" in ps) echo abc123; echo def456 ;; esac'
    sb = sandbox(tmp_path, "podman", ps)
    assert sb.cleanup_orphans() == 2
    log = calls(tmp_path, "podman")
    assert ["ps", "-a", "-q", "--filter", "label=maef.run"] in log
    assert ["rm", "-f", "-t", "0", "abc123"] in log and ["rm", "-f", "-t", "0", "def456"] in log


def test_cleanup_orphans_with_no_labelled_containers_removes_nothing(tmp_path):
    sb = sandbox(tmp_path, "podman")
    assert sb.cleanup_orphans() == 0
    assert not any(c[:2] == ["rm", "-f"] for c in calls(tmp_path, "podman"))
