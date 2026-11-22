import os
import tempfile

import pytest

# Import-time ledger creation must not touch the real runs/ directory.
os.environ.setdefault("MAEF_DB", os.path.join(tempfile.mkdtemp(prefix="maef-test-"), "maef.db"))
os.environ.setdefault("MAEF_ARTIFACTS_DIR", os.path.join(tempfile.mkdtemp(prefix="maef-test-art-"), "artifacts"))


# --- Auth for the suite -------------------------------------------------------------------------
# Every Ledger created during a test run is seeded with one admin token (TEST_ADMIN_TOKEN, name
# "test-admin"), starlette's TestClient sends it by default, and MAEF_API_TOKEN carries it for
# MaefClient/HttpTransport/AsgiTransport. A test that wants an unauthenticated request passes its own
# `headers={"Authorization": ...}` to the TestClient or removes `client.headers["Authorization"]`.
TEST_ADMIN_TOKEN = "maef_test-admin-token-not-a-secret"
os.environ["MAEF_API_TOKEN"] = TEST_ADMIN_TOKEN


def _install_test_auth() -> None:
    from fastapi.testclient import TestClient
    from maef_api.auth import hash_token
    from maef_api.ledger import Ledger

    original_ledger_init = Ledger.__init__

    def ledger_init(self, *args, **kwargs):
        original_ledger_init(self, *args, **kwargs)
        self.register_api_token("test-admin", ("admin",), hash_token(TEST_ADMIN_TOKEN))

    Ledger.__init__ = ledger_init

    original_client_init = TestClient.__init__

    def client_init(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Authorization", f"Bearer {TEST_ADMIN_TOKEN}")
        original_client_init(self, *args, headers=headers, **kwargs)

    TestClient.__init__ = client_init
    # The API's own ledger was created at import time, before the patch above.
    from maef_api import main
    main.ledger.register_api_token("test-admin", ("admin",), hash_token(TEST_ADMIN_TOKEN))


_install_test_auth()


def copy_research_analyst(dest):
    """Copy the shipped research-analyst without its MCP server (needs a container runtime to run)."""
    import shutil

    from maef_api.registry import AgentRegistry
    shutil.copytree(AgentRegistry().root / 'research-analyst', dest / 'research-analyst')
    path = dest / 'research-analyst' / 'agent.yaml'
    text = path.read_text().replace('mcp_servers: ["fetch@0.3.0"]', 'mcp_servers: []')
    path.write_text(text.replace('permissions:\n  network_open: true\n', ''))


class FakeAdapter:
    """Stands in for the model: replies with `text`, or raises `error` when set. Records every call."""

    def __init__(self, text='Final answer.'):
        self.text, self.error, self.calls = text, None, []

    def converse(self, **kwargs):
        from maef_api.adapters.base import ModelResponse
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ModelResponse(self.text, (), 'end_turn', 100, 10)


class LiveApi:
    def __init__(self, transport, ledger, adapter, worker):
        self.transport, self.ledger, self.adapter, self.worker = transport, ledger, adapter, worker

    def finish_queued(self) -> bool:
        """Claim and run one queued run on the calling thread."""
        return self.worker.run_once()

    def finish_later(self, delay: float = 0.1):
        """Claim and run one queued run on a background thread after `delay` seconds."""
        import threading
        import time

        def go():
            time.sleep(delay)
            self.worker.run_once()

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread


@pytest.fixture
def live_api(tmp_path, monkeypatch):
    """The real FastAPI app behind AsgiTransport: execution enabled, tmp ledger and blob store, a
    `local-default` profile dir, research-analyst pinned, and a Worker driving FakeAdapter.
    Event streams are shrunk so wait() and the SSE tests stay fast."""
    from maef_api import main
    from maef_api.artifacts import LocalArtifactStore
    from maef_api.channel import LocalChannel
    from maef_api.client import AsgiTransport
    from maef_api.definitions import compute_definition_digest
    from maef_api.ledger import Ledger
    from maef_api.registry import ModelProfileRegistry
    from maef_api.runs import Runs
    from maef_api.worker import Worker
    from test_api import PROFILE
    from test_run_execution import definition

    store = Ledger(tmp_path / 'live.db')
    store.ensure_account('default', 5_000_000)
    blobs = LocalArtifactStore(tmp_path / 'blobs')
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    (profiles / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra=''))

    monkeypatch.setenv('MAEF_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('MAEF_ALLOW_DIRTY', '1')
    pinned = definition(dirty=True)
    pinned = pinned.model_copy(update={'definition_digest': compute_definition_digest(pinned.definition_bytes, pinned.files)})
    monkeypatch.setattr(main.runs.definitions, 'resolve', lambda agent_id: pinned)
    monkeypatch.setattr(main, 'ledger', store)
    monkeypatch.setattr(main, 'artifacts', blobs)
    monkeypatch.setattr(main, 'model_profiles', ModelProfileRegistry(profiles))
    monkeypatch.setattr(main, 'runs', Runs(main.runs.definitions, store,
                                           execution_enabled=main.execution_enabled, allow_dirty=main.allow_dirty))
    monkeypatch.setattr(main.sse, 'poll_seconds', 0.01)
    monkeypatch.setattr(main.sse, 'keepalive_seconds', 0.01)
    monkeypatch.setattr(main.sse, 'max_seconds', 0.5)  # each stream closes after this; wait() reconnects

    adapter = FakeAdapter()
    worker = Worker(LocalChannel(store, artifacts=blobs), lambda profile: adapter, worker_id='w1')
    return LiveApi(AsgiTransport(main.app), store, adapter, worker)


# --- Ledger backends: SQLite and PostgreSQL ------------------------------------------------------
# Ledger-level fixtures take `ledger_target` and call `Ledger(ledger_target('name.db'))`; every test using
# them runs once per backend. Postgres needs MAEF_TEST_POSTGRES_URL (any database on a server the test
# role may CREATE DATABASE on) or rootless podman, and skips otherwise. Each target is a fresh database.
POSTGRES_IMAGE = ('docker.io/library/postgres@sha256:'
                  'f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73')  # postgres:17-alpine


def _wait_for_postgres(url: str, timeout: float = 60.0) -> None:
    import time

    import psycopg
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute('SELECT 1')
            return
        except psycopg.Error as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f'postgres not ready after {timeout}s: {last}')


@pytest.fixture(scope='session')
def postgres_url():
    """Admin URL of a PostgreSQL server: MAEF_TEST_POSTGRES_URL, else a throwaway podman container."""
    import secrets
    import shutil
    import socket
    import subprocess

    pytest.importorskip('psycopg')
    given = os.environ.get('MAEF_TEST_POSTGRES_URL')
    if given:
        _wait_for_postgres(given)
        yield given
        return
    podman = shutil.which('podman')
    if podman is None:
        pytest.skip('postgres: neither MAEF_TEST_POSTGRES_URL nor podman is available')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    password = secrets.token_hex(12)
    started = subprocess.run(
        [podman, 'run', '-d', '--rm', '-p', f'127.0.0.1:{port}:5432', '-e', f'POSTGRES_PASSWORD={password}',
         POSTGRES_IMAGE, 'postgres', '-c', 'fsync=off', '-c', 'max_connections=300'],
        capture_output=True, text=True, check=False)
    if started.returncode != 0:
        pytest.skip(f'postgres: podman could not start the container: {started.stderr.strip()[:200]}')
    container = started.stdout.strip()
    url = f'postgresql://postgres:{password}@127.0.0.1:{port}/postgres'
    try:
        _wait_for_postgres(url)
        yield url
    finally:
        subprocess.run([podman, 'rm', '-f', container], capture_output=True, check=False)


@pytest.fixture
def fresh_postgres_url(postgres_url):
    """A factory of URLs, each naming a new empty database; all are dropped at teardown."""
    import uuid
    from urllib.parse import urlsplit, urlunsplit

    import psycopg
    made = []

    def make() -> str:
        name = 'maef_' + uuid.uuid4().hex[:16]
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE {name}')
        made.append(name)
        return urlunsplit(urlsplit(postgres_url)._replace(path='/' + name))

    yield make
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        for name in made:
            conn.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


@pytest.fixture(params=['sqlite', pytest.param('postgres', marks=pytest.mark.pg)])
def backend(request):
    return request.param


@pytest.fixture
def ledger_target(backend, tmp_path, request):
    """`ledger_target('x.db')`: a SQLite path under tmp_path, or a fresh Postgres database URL."""
    if backend == 'sqlite':
        return lambda name='ledger.db': tmp_path / name
    make = request.getfixturevalue('fresh_postgres_url')
    return lambda name='ledger.db': make()
