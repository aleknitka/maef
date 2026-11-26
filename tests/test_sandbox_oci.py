"""Real-runtime sandbox tests (podman preferred, docker accepted). Skipped when no runtime is installed.

Run with `make test-oci`. The image is pulled on first use, so this needs registry access once.
require_rootless is relaxed only when the available runtime is not rootless (for example Docker Desktop
in WSL2); the sandbox itself still enforces every other restriction.
"""

import subprocess
import uuid

import pytest

from maef_api.registry import SandboxProfileRegistry
from maef_api.sandbox import SandboxError, detect_sandbox

SANDBOX = detect_sandbox()
pytestmark = [pytest.mark.oci, pytest.mark.skipif(SANDBOX is None, reason="no podman or docker runtime found")]


@pytest.fixture
def session(monkeypatch):
    profile = SandboxProfileRegistry().get_profile("restricted-local")
    if not SANDBOX.info()["rootless"]:
        profile = profile.model_copy(update={"require_rootless": False})  # CI runtime is rootful
    run_id = uuid.uuid4().hex
    s = SANDBOX.provision(profile, run_id)
    s.run_id = run_id
    yield s
    s.teardown()


def sh(session, script, timeout=20):
    return session.exec(["sh", "-c", script], timeout=timeout)


def test_exec_echo(session):
    r = session.exec(["echo", "hello"], timeout=20)
    assert (r.exit_code, r.stdout.strip(), r.timed_out) == (0, b"hello", False)


def test_exec_stdin_and_timeout(session):
    assert session.exec(["cat"], stdin=b"abc", timeout=20).stdout == b"abc"
    r = session.exec(["sleep", "30"], timeout=1)
    assert r.timed_out


def test_root_filesystem_is_read_only(session):
    assert sh(session, "echo x > /etc/maef-test").exit_code != 0
    assert sh(session, "echo x > /workspace/ok").exit_code == 0


def test_network_egress_denied(session):
    assert sh(session, "wget -T 2 -q -O- http://1.1.1.1").exit_code != 0


def test_no_capabilities_and_unprivileged_user(session):
    caps = sh(session, "grep CapEff /proc/self/status").stdout.decode().strip()
    assert caps.endswith("0000000000000000")
    assert sh(session, "id -u").stdout.strip() == b"65534"


def test_pids_limit_is_enforced(session):
    r = sh(session, "for i in $(seq 200); do sleep 5 & done; wait", timeout=30)
    assert r.exit_code != 0 or b"ork" in r.stderr  # fork failures bound the process count


def test_put_get_round_trip_and_path_confinement(session):
    session.put("/workspace/sub/a.txt", b"data\x00bytes")
    assert session.get("/workspace/sub/a.txt") == b"data\x00bytes"
    for bad in ("/etc/passwd", "/workspace/../etc/passwd", "a.txt"):
        with pytest.raises(ValueError):
            session.put(bad, b"x")
        with pytest.raises(ValueError):
            session.get(bad)
    with pytest.raises(SandboxError):
        session.get("/workspace/missing")


def test_teardown_removes_container(session):
    rt = SANDBOX.runtime

    def listed():
        out = subprocess.run(
            [rt, "ps", "-a", "-q", "--filter", f"label=maef.run={session.run_id}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    assert listed()
    session.teardown()
    session.teardown()  # idempotent
    assert listed() == ""


# --- enforced egress proxy (network="allowlist") ---

from maef_api.sandbox import EgressSpec


def _rt(*args):
    return subprocess.run([SANDBOX.runtime, *args], capture_output=True, text=True, check=False)


def _labelled(run_id):
    out = []
    for kind in (["ps", "-a", "-q"], ["network", "ls", "-q"]):
        out += _rt(*kind, "--filter", f"label=maef.run={run_id}").stdout.split()
    return out


@pytest.fixture
def egress_session():
    """A python client container (the pinned proxy image) on the per-run internal network."""
    from maef_api.sandbox import EGRESS_PROXY_IMAGE

    profile = (
        SandboxProfileRegistry()
        .get_profile("restricted-local")
        .model_copy(update={"image": EGRESS_PROXY_IMAGE, "user": "65534:65534"})
    )
    if not SANDBOX.info()["rootless"]:
        profile = profile.model_copy(update={"require_rootless": False})
    run_id = uuid.uuid4().hex
    s = SANDBOX.provision(
        profile,
        run_id,
        network="allowlist",
        egress=EgressSpec(
            hosts=("example.com", "*.example.org", "localhost", "127.0.0.1", "169.254.169.254", "10.0.0.1")
        ),
    )
    s.run_id = run_id
    yield s
    s.teardown()
    assert _labelled(run_id) == []  # no labelled containers or networks survive teardown


CLIENT = """
import os, socket, sys
host, port = sys.argv[1], int(sys.argv[2])
if len(sys.argv) > 3 and sys.argv[3] == "direct":
    s = socket.create_connection((host, port), timeout=4); print("CONNECTED"); sys.exit(0)
p = os.environ["HTTPS_PROXY"].split("//")[1].split(":")
s = socket.create_connection((p[0], int(p[1])), timeout=15)
s.sendall(f"CONNECT {host}:{port} HTTP/1.1\\r\\nHost: {host}:{port}\\r\\n\\r\\n".encode())
print(s.recv(200).decode().split("\\r\\n")[0])
"""


def connect(session, host, port=443, mode=""):
    r = session.exec(["python", "-c", CLIENT, host, str(port), mode], timeout=40)
    return r.exit_code, r.stdout.decode().strip()


def test_proxy_env_and_internal_network_only(egress_session):
    env = sh(egress_session, "env")
    assert b"HTTPS_PROXY=http://10." in env.stdout and b"NO_PROXY=\n" in env.stdout
    assert (
        _rt(
            "network",
            "inspect",
            "--format",
            "{{.Internal}}",
            _rt("network", "ls", "-q", "--filter", f"label=maef.run={egress_session.run_id}").stdout.split()[0],
        ).stdout.strip()
        == "true"
    )


def test_allowlisted_host_connects_through_proxy_only(egress_session):
    _code, out = connect(egress_session, "example.com")
    if "502" in out:
        pytest.skip("no internet access from the proxy")
    assert out.startswith("HTTP/1.1 200"), out
    # the same target without the proxy has no route
    assert connect(egress_session, "93.184.216.34", 443, "direct")[1] != "CONNECTED"
    assert connect(egress_session, "1.1.1.1", 443, "direct")[1] != "CONNECTED"


def test_non_allowlisted_host_is_denied(egress_session):
    assert connect(egress_session, "github.com")[1].startswith("HTTP/1.1 403")
    assert connect(egress_session, "example.com", 8080)[1].startswith("HTTP/1.1 403")


@pytest.mark.parametrize("host", ["169.254.169.254", "10.0.0.1", "127.0.0.1", "localhost"])
def test_private_targets_denied_even_when_allowlisted(egress_session, host):
    assert connect(egress_session, host)[1].startswith("HTTP/1.1 403")


def test_wildcard_all_still_refuses_private_addresses():
    profile = SandboxProfileRegistry().get_profile("restricted-local")
    from maef_api.sandbox import EGRESS_PROXY_IMAGE

    profile = profile.model_copy(update={"image": EGRESS_PROXY_IMAGE, "require_rootless": SANDBOX.info()["rootless"]})
    run_id = uuid.uuid4().hex
    s = SANDBOX.provision(profile, run_id, network="allowlist", egress=EgressSpec(hosts=("*",)))
    try:
        for host in ("169.254.169.254", "10.0.0.1", "127.0.0.1", "localhost", "2130706433", "[::1]"):
            assert connect(s, host)[1].startswith("HTTP/1.1 403"), host
        summary = s.egress_summary()
        assert summary["decisions"].get("deny", 0) >= 5 and "allow" not in summary["decisions"]
    finally:
        s.teardown()
    assert _labelled(run_id) == []


def test_cleanup_orphans_removes_labelled_networks(egress_session):
    assert _labelled(egress_session.run_id)
    SANDBOX.cleanup_orphans()
    assert _labelled(egress_session.run_id) == []
