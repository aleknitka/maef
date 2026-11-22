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
    text = path.read_text().replace('mcp_servers: ["fetch@0.1.0"]', 'mcp_servers: []')
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
