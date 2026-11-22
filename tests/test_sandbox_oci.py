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
pytestmark = [pytest.mark.oci, pytest.mark.skipif(SANDBOX is None, reason='no podman or docker runtime found')]


@pytest.fixture
def session(monkeypatch):
    profile = SandboxProfileRegistry().get_profile('restricted-local')
    if not SANDBOX.info()['rootless']:
        profile = profile.model_copy(update={'require_rootless': False})  # CI runtime is rootful
    run_id = uuid.uuid4().hex
    s = SANDBOX.provision(profile, run_id)
    s.run_id = run_id
    yield s
    s.teardown()


def sh(session, script, timeout=20):
    return session.exec(['sh', '-c', script], timeout=timeout)


def test_exec_echo(session):
    r = session.exec(['echo', 'hello'], timeout=20)
    assert (r.exit_code, r.stdout.strip(), r.timed_out) == (0, b'hello', False)


def test_exec_stdin_and_timeout(session):
    assert session.exec(['cat'], stdin=b'abc', timeout=20).stdout == b'abc'
    r = session.exec(['sleep', '30'], timeout=1)
    assert r.timed_out


def test_root_filesystem_is_read_only(session):
    assert sh(session, 'echo x > /etc/maef-test').exit_code != 0
    assert sh(session, 'echo x > /workspace/ok').exit_code == 0


def test_network_egress_denied(session):
    assert sh(session, 'wget -T 2 -q -O- http://1.1.1.1').exit_code != 0


def test_no_capabilities_and_unprivileged_user(session):
    caps = sh(session, 'grep CapEff /proc/self/status').stdout.decode().strip()
    assert caps.endswith('0000000000000000')
    assert sh(session, 'id -u').stdout.strip() == b'65534'


def test_pids_limit_is_enforced(session):
    r = sh(session, 'for i in $(seq 200); do sleep 5 & done; wait', timeout=30)
    assert r.exit_code != 0 or b'ork' in r.stderr  # fork failures bound the process count


def test_put_get_round_trip_and_path_confinement(session):
    session.put('/workspace/sub/a.txt', b'data\x00bytes')
    assert session.get('/workspace/sub/a.txt') == b'data\x00bytes'
    for bad in ('/etc/passwd', '/workspace/../etc/passwd', 'a.txt'):
        with pytest.raises(ValueError):
            session.put(bad, b'x')
        with pytest.raises(ValueError):
            session.get(bad)
    with pytest.raises(SandboxError):
        session.get('/workspace/missing')


def test_teardown_removes_container(session):
    rt = SANDBOX.runtime

    def listed():
        out = subprocess.run([rt, 'ps', '-a', '-q', '--filter', f'label=maef.run={session.run_id}'],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    assert listed()
    session.teardown()
    session.teardown()  # idempotent
    assert listed() == ''
