import base64
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from maef_api import main
from maef_api.adapters.base import ModelResponse
from maef_api.auth import hash_token
from maef_api.channel import HttpChannel, LocalChannel
from maef_api.credentials import Secret
from maef_api.definitions import compute_definition_digest
from maef_api.leases import FencingError
from maef_api.ledger import Ledger
from maef_api.models import Budget
from maef_api.runs import Runs
from maef_api.worker import Worker
from test_run_execution import definition

TOKEN = 'maefw_test-worker-credential'  # registered for worker 'w1' by the `api`/`http` fixtures
AUTH = {'Authorization': f'Bearer {TOKEN}'}


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(text='answer', tool_calls=(), stop_reason='end_turn', input_tokens=7, output_tokens=5)


class TamperingChannel(LocalChannel):
    """Hands the worker a lease whose pinned files differ from what was admitted."""

    def claim(self, worker_id):
        lease = super().claim(worker_id)
        if lease is None:
            return None
        pinned = lease.task.pinned.model_copy(update={'files': {'SYSTEM.md': b'changed after admission'}})
        return lease.model_copy(update={'task': lease.task.model_copy(update={'pinned': pinned})})


class FencedReportChannel(LocalChannel):
    """Every report is refused as stale; records what the worker tried to call."""

    def __init__(self, ledger):
        super().__init__(ledger)
        self.calls = []

    def report(self, lease, wseq, type, payload=None, status=None):
        self.calls.append(('report', wseq, type))
        raise FencingError('stale')

    def finish(self, lease, **kwargs):
        self.calls.append(('finish',))
        super().finish(lease, **kwargs)


def pinned(files=None):
    d = definition()
    files = files or {}
    return d.model_copy(update={'files': files, 'definition_digest': compute_definition_digest(d.definition_bytes, files)})


@pytest.fixture
def store(ledger_target):
    s = Ledger(ledger_target('worker.db'))
    s.ensure_account('default', 1_000_000)
    return s


@pytest.fixture
def api(store, monkeypatch):
    store.register_worker_credential('w1', hash_token(TOKEN))
    monkeypatch.setattr(main, 'ledger', store)
    monkeypatch.setattr(main, 'runs', Runs(main.runs.definitions, store,
                                           execution_enabled=lambda: True, allow_dirty=lambda: True))
    return TestClient(main.app)


def admit(store, files=None, instruction='Question?'):
    return store.admit(pinned(files), Budget(max_tokens=500_000, max_turns=1), prompt_bytes=28, instruction=instruction)


def types(store, run_id):
    return [e.type for e in store.get(run_id).events]


def run_once(channel, adapter):
    return Worker(channel, lambda profile: adapter, worker_id='w1').run_once()


# --- Worker ---

def test_run_once_executes_admitted_run(store):
    run_id = admit(store)
    adapter = FakeAdapter()
    assert run_once(LocalChannel(store), adapter) is True
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason, detail.tokens_used) == ('succeeded', 'completed', 12)
    assert len(adapter.calls) == 1


def test_run_once_on_empty_queue_is_false(store):
    adapter = FakeAdapter()
    assert run_once(LocalChannel(store), adapter) is False
    assert adapter.calls == []


def test_tampered_definition_is_rejected_before_the_adapter(store):
    run_id = admit(store, files={'SYSTEM.md': b'original'})
    adapter = FakeAdapter()
    assert run_once(TamperingChannel(store), adapter) is True
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason) == ('failed', 'definition_mismatch')
    assert adapter.calls == []


def test_adapter_factory_error_fails_run_with_class_name_only(store):
    run_id = admit(store)

    def broken(profile):
        raise RuntimeError('api-key=SECRET-VALUE')

    assert Worker(LocalChannel(store), broken, worker_id='w1').run_once() is True
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason) == ('failed', 'setup_error:RuntimeError')
    assert 'SECRET-VALUE' not in json.dumps([e.model_dump() for e in detail.events])


def test_fenced_report_stops_harness_without_finish(store):
    run_id = admit(store)
    channel = FencedReportChannel(store)
    adapter = FakeAdapter()
    assert run_once(channel, adapter) is True
    assert channel.calls == [('report', 1, 'run.started')]
    assert adapter.calls == []
    assert store.get(run_id).status == 'leased'


# --- HTTP routes ---

def test_claim_refused_when_worker_credential_revoked(api, store):
    store.revoke_worker('w1')
    assert api.post('/api/v1/worker/claim', json={'worker_id': 'w1'}, headers=AUTH).status_code == 401


def test_claim_with_wrong_bearer_is_401(api):
    body = {'worker_id': 'w1'}
    assert api.post('/api/v1/worker/claim', json=body).status_code == 401
    assert api.post('/api/v1/worker/claim', json=body, headers={'Authorization': 'Bearer nope'}).status_code == 401


def test_claim_returns_lease_then_204(api, store):
    run_id = admit(store)
    res = api.post('/api/v1/worker/claim', json={'worker_id': 'w1'}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body['run_id'] == run_id and body['fencing_token'] == 1 and 'task' in body
    empty = api.post('/api/v1/worker/claim', json={'worker_id': 'w1'}, headers=AUTH)
    assert empty.status_code == 204 and empty.content == b''


def test_stale_token_gets_fencing_conflict(api, store):
    run_id = admit(store)
    lease = LocalChannel(store).claim('w1')
    res = api.post(f'/api/v1/worker/runs/{run_id}/events', headers=AUTH, json={
        'fencing_token': lease.fencing_token + 9, 'wseq': 1, 'type': 'run.started', 'payload': {}})
    assert res.status_code == 409 and res.json()['detail']['code'] == 'fencing'


def test_sequence_gap_gets_conflict(api, store):
    run_id = admit(store)
    lease = LocalChannel(store).claim('w1')
    res = api.post(f'/api/v1/worker/runs/{run_id}/events', headers=AUTH, json={
        'fencing_token': lease.fencing_token, 'wseq': 3, 'type': 'run.started', 'payload': {}})
    assert res.status_code == 409 and res.json()['detail']['code'] == 'sequence_gap'


def test_run_detail_never_exposes_task(api, store):
    run_id = admit(store, files={'SYSTEM.md': b'FILE-MARKER'}, instruction='INSTRUCTION-MARKER')
    res = api.get(f'/api/v1/runs/{run_id}')
    assert res.status_code == 200
    assert 'task' not in res.json()
    for secret in ('INSTRUCTION-MARKER', 'FILE-MARKER', base64.b64encode(b'FILE-MARKER').decode(), 'You are an analyst'):
        assert secret not in res.text


# --- HttpChannel end to end ---

def start_server(app):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', lifespan='off'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            pytest.skip('uvicorn did not start within 5s')
        time.sleep(0.02)
    return server, thread, port


def test_http_worker_runs_admitted_run(api, store):
    run_id = admit(store)
    server, thread, port = start_server(main.app)
    try:
        channel = HttpChannel(f'http://127.0.0.1:{port}', Secret(TOKEN))
        assert Worker(channel, lambda profile: FakeAdapter(), worker_id='w1').run_once() is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert store.get(run_id).status == 'succeeded'


# --- Sandbox integration (fake adapter) ---

from maef_api.models import AgentDefinition, SandboxProfile
from maef_api.sandbox import ExecResult, FakeSandbox

SANDBOX = SandboxProfile(schema_version='maef.dev/v1alpha1', id='restricted-local', image='docker.io/library/busybox:1.37',
                         cpus=1, memory_mb=64, pids_limit=32, workspace_mb=8)


def admit_sandboxed(store, sandbox=SANDBOX):
    d = definition()
    agent = AgentDefinition(**{**d.agent.model_dump(), 'tools': ['run_command']})
    d = d.model_copy(update={'agent': agent, 'sandbox': sandbox, 'definition_digest': compute_definition_digest(b'', {})})
    return store.admit(d, Budget(max_tokens=500_000, max_turns=2), prompt_bytes=28, instruction='go')


class ToolThenAnswer:
    def __init__(self):
        self.n = 0

    def converse(self, **kw):
        self.n += 1
        if self.n == 1:
            from maef_api.adapters.base import ToolCall
            return ModelResponse(text='', tool_calls=(ToolCall('c1', 'run_command', {'argv': ['echo', 'hi']}),),
                                 stop_reason='tool_use', input_tokens=3, output_tokens=2)
        return ModelResponse(text='done', tool_calls=(), stop_reason='end_turn', input_tokens=3, output_tokens=2)


def test_sandboxed_run_provisions_executes_and_tears_down(store):
    run_id = admit_sandboxed(store)
    sandbox = FakeSandbox(lambda argv, stdin: ExecResult(0, b'hi\n', b''))
    worker = Worker(LocalChannel(store), lambda p: ToolThenAnswer(), worker_id='w1', sandbox=sandbox)
    assert worker.run_once() is True
    detail = store.get(run_id)
    assert detail.status == 'succeeded'
    ev = {e.type: e.payload for e in detail.events}
    assert ev['sandbox.provisioned']['profile'] == 'restricted-local'
    assert ev['sandbox.provisioned']['unpinned_image_allowed'] is True
    assert 'sandbox.torn_down' in ev
    session = sandbox.sessions[0]
    assert session.execs[0] == ['echo', 'hi'] and session.execs[1][0] == 'find' and len(session.execs) == 2
    assert session.torn_down
    assert 0 < session.timeouts[0] <= 30


@pytest.mark.parametrize('sandbox', [None, FakeSandbox(fail='runtime_not_rootless')])
def test_sandboxed_run_without_usable_sandbox_fails_before_model_call(store, sandbox):
    run_id = admit_sandboxed(store)
    adapter = FakeAdapter()
    assert Worker(LocalChannel(store), lambda p: adapter, worker_id='w1', sandbox=sandbox).run_once() is True
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason) == ('failed', 'sandbox_unavailable')
    assert adapter.calls == []
