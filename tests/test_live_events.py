"""Run events: the ledger cursor and the SSE route. Frames must never carry instruction text or tool arguments."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from maef_api import main
from maef_api.adapters.base import ModelResponse, ToolCall
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.ledger import Ledger
from maef_api.models import Budget
from maef_api.runs import Runs
from maef_api.tools import BuiltinTools
from test_run_execution import definition

BUDGET = Budget(max_tokens=500_000, max_turns=2)


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / 'events.db')
    s.ensure_account('default', 1_000_000)
    return s


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(main, 'runs', Runs(main.runs.definitions, store,
                                           execution_enabled=lambda: True, allow_dirty=lambda: True))
    monkeypatch.setattr(main.sse, 'poll_seconds', 0.01)
    monkeypatch.setattr(main.sse, 'keepalive_seconds', 0.01)
    monkeypatch.setattr(main.sse, 'max_seconds', 5.0)  # a hung stream fails instead of hanging the suite
    return TestClient(main.app)


def admit(store, instruction='Question?'):
    return store.admit(definition(), BUDGET, prompt_bytes=28, instruction=instruction)


def finish(store, run_id, status='succeeded'):
    store.settle(run_id, tokens_used=0, status=status, terminal_reason='completed')


def stream(client, run_id, query='', headers=None):
    with client.stream('GET', f'/api/v1/runs/{run_id}/events{query}', headers=headers or {}) as res:
        return res, res.read().decode()


def frames(body):
    """Event frames only, as {id, event, data}; keepalive comments are counted by comments()."""
    out = []
    for block in filter(None, body.split('\n\n')):
        if block.startswith(':'):
            continue
        fields = dict(line.split(': ', 1) for line in block.split('\n'))
        out.append({'id': int(fields['id']), 'event': fields['event'], 'data': json.loads(fields['data'])})
    return out


def comments(body):
    return sum(1 for block in body.split('\n\n') if block.startswith(': keepalive'))


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)

    def converse(self, **kwargs):
        return self.replies.pop(0)


# --- Ledger cursor ---

def test_events_after_is_an_ordered_cursor_honouring_limit(store):
    run = admit(store)
    store.append(run, 'model.request', {'turn': 1})
    store.append(run, 'model.response', {'turn': 1})
    other = admit(store)
    everything = store.events_after(run, 0)
    assert [e.seq for e in everything] == [1, 2, 3, 4]
    assert [e.type for e in everything] == ['task.created', 'task.admitted', 'model.request', 'model.response']
    assert all(e.run_id == run and isinstance(e.payload, dict) for e in everything)
    assert [e.seq for e in store.events_after(run, 2)] == [3, 4]
    assert [e.seq for e in store.events_after(run, 0, limit=2)] == [1, 2]
    assert [e.seq for e in store.events_after(run, 1, limit=2)] == [2, 3]
    assert store.events_after(run, 4) == []
    assert [e.run_id for e in store.events_after(other, 0)] == [other, other]


# --- SSE route ---

def test_terminal_run_streams_all_events_then_closes(store, client):
    run = admit(store)
    store.append(run, 'model.request', {'turn': 1})
    finish(store, run)
    res, body = stream(client, run)
    assert res.status_code == 200
    assert res.headers['cache-control'] == 'no-store'
    assert res.headers['content-type'].startswith('text/event-stream')
    got = frames(body)
    assert [f['id'] for f in got] == [1, 2, 3, 4]
    assert [f['event'] for f in got] == ['task.created', 'task.admitted', 'model.request', 'task.succeeded']
    for f in got:
        assert set(f['data']) == {'seq', 'type', 'ts', 'payload'}
        assert f['data']['seq'] == f['id'] and f['data']['type'] == f['event']
    assert body.startswith('id: 1\nevent: task.created\ndata: {')


def test_after_query_skips_earlier_events(store, client):
    run = admit(store)
    store.append(run, 'model.request', {'turn': 1})
    finish(store, run)
    _, body = stream(client, run, query='?after=2')
    assert [f['id'] for f in frames(body)] == [3, 4]


def test_last_event_id_header_wins_over_after(store, client):
    run = admit(store)
    store.append(run, 'model.request', {'turn': 1})
    finish(store, run)
    _, body = stream(client, run, query='?after=1', headers={'Last-Event-ID': '3'})
    assert [f['id'] for f in frames(body)] == [4]


def test_unknown_run_is_404(client):
    assert client.get('/api/v1/runs/no-such-run/events').status_code == 404


def test_running_run_delivers_later_events_then_closes(store, client):
    run = admit(store)
    store.append(run, 'run.started', status='running')

    def work():
        time.sleep(0.05)
        store.append(run, 'model.request', {'turn': 1})
        time.sleep(0.05)
        store.append(run, 'model.response', {'turn': 1})
        finish(store, run)

    worker = threading.Thread(target=work)
    worker.start()
    _, body = stream(client, run)
    worker.join()
    assert [f['event'] for f in frames(body)] == [
        'task.created', 'task.admitted', 'run.started', 'model.request', 'model.response', 'task.succeeded']


def test_idle_run_emits_keepalive_comments(store, client):
    run = admit(store)
    worker = threading.Thread(target=lambda: (time.sleep(0.15), finish(store, run)))
    worker.start()
    _, body = stream(client, run)
    worker.join()
    assert comments(body) >= 1
    assert frames(body)[-1]['event'] == 'task.succeeded'


def test_frames_never_carry_instruction_or_tool_arguments(store, client):
    run = admit(store, instruction='INSTRUCTION-MARKER-7')
    pinned = definition()
    tools = BuiltinTools(['read_agent_file'], {'SYSTEM.md': b'system text'})
    adapter = Scripted(
        ModelResponse('', (ToolCall('c1', 'read_agent_file', {'path': 'ARG-MARKER-9.md'}),), 'tool_use', 3, 2),
        ModelResponse('done', (), 'end_turn', 3, 2))
    prepared = PreparedRun(run, pinned, BUDGET, store.get(run).reserved_tokens, 'INSTRUCTION-MARKER-7', tools)
    execute(LedgerSink(store, run), prepared, adapter)
    _, body = stream(client, run)
    assert 'tool.call' in [f['event'] for f in frames(body)]
    for marker in ('INSTRUCTION-MARKER-7', 'ARG-MARKER-9', 'system text', 'You are an analyst'):
        assert marker not in body
