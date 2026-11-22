import sqlite3
import threading

import pytest
import yaml
from fastapi.testclient import TestClient
from maef_api import main
from maef_api.definitions import PinnedDefinition
from maef_api.ledger import AdmissionError, Ledger
from maef_api.models import AgentDefinition, Budget, ModelEndpointProfile
from maef_api.runs import Runs
from test_api import PROFILE


def definition(dirty=False, max_output_tokens=1000):
    agent = AgentDefinition(schema_version='maef.dev/v1alpha1', id='research-analyst', version='0.1.0',
                            display_name='R', description='d')
    profile = ModelEndpointProfile.model_validate(
        yaml.safe_load(PROFILE.format(id='test-model', extra=f'max_output_tokens: {max_output_tokens}\n')))
    return PinnedDefinition(agent=agent, profile=profile, instructions='sys', git_rev='abc', dirty=dirty,
                            definition_digest='d', profile_digest='p')

@pytest.fixture
def store(ledger_target):
    s = Ledger(ledger_target('ledger.db'))
    s.ensure_account('default', 1_000_000)
    return s

def run(store, budget=None, **kw):
    kw.setdefault('definition', definition())
    return store.admit(kw.pop('definition'), budget or Budget(max_tokens=400_000, max_turns=1),
                       prompt_bytes=105, instruction='Question?', **kw)

def test_admit_reserves_and_logs_events(store):
    run_id = run(store)
    detail = store.get(run_id)
    assert [e.type for e in detail.events] == ['task.created', 'task.admitted']
    assert [e.seq for e in detail.events] == [1, 2]
    # worst case is far below the cap: 105 prompt tokens + 1000 output tokens
    assert detail.reserved_tokens == 1105
    assert store.account('default')['reserved_tokens'] == 1105

def test_dirty_refused_unless_flagged(store):
    with pytest.raises(AdmissionError) as e:
        run(store, definition=definition(dirty=True))
    assert e.value.code == 'dirty'
    run_id = run(store, definition=definition(dirty=True), allow_dirty=True)
    assert store.get(run_id).events[0].payload['dirty_allowed'] is True

def test_settle_releases_unused(store):
    run_id = run(store, budget=Budget(max_tokens=400_000, max_turns=100))
    reserved = store.get(run_id).reserved_tokens
    assert reserved > 100
    store.settle(run_id, tokens_used=100, status='succeeded', terminal_reason='completed')
    acct = store.account('default')
    assert (acct['reserved_tokens'], acct['spent_tokens']) == (0, 100)
    detail = store.get(run_id)
    assert detail.status == 'succeeded' and detail.events[-1].payload['released_tokens'] == reserved - 100
    with pytest.raises(AdmissionError):
        store.settle(run_id, tokens_used=1, status='succeeded', terminal_reason='again')

def test_release_returns_everything(store):
    run_id = run(store)
    store.release(run_id, 'cancelled_before_start')
    acct = store.account('default')
    assert (acct['reserved_tokens'], acct['spent_tokens']) == (0, 0)

def test_concurrent_admission_exactly_one_fits(ledger_target):
    store = Ledger(ledger_target('c.db'))
    store.ensure_account('default', 500_000)  # fits one 400k reservation, not two
    # large max_turns/outputs push the worst case above the cap, so each reserves 400k
    budget = Budget(max_tokens=400_000, max_turns=100)
    results, barrier = [], threading.Barrier(4)
    def worker():
        barrier.wait()
        try:
            store.admit(definition(max_output_tokens=100_000), budget, prompt_bytes=11, instruction='Q')
            results.append('ok')
        except AdmissionError as e:
            results.append(e.code)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(results) == ['insufficient_budget'] * 3 + ['ok']
    assert len(store.list()) == 1

def test_events_are_append_only(store):
    run_id = run(store)
    with store.connect() as conn:
        with pytest.raises(store.database_error, match='append-only'):
            conn.execute("UPDATE run_events SET type='x' WHERE run_id=?", (run_id,))
        with pytest.raises(store.database_error, match='append-only'):
            conn.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))

def test_failed_admission_leaves_no_partial_state(store):
    with pytest.raises(AdmissionError):
        run(store, budget=Budget(max_tokens=5_000_000, max_turns=100), account_id='missing')
    assert store.list() == []

def test_runs_api(store, monkeypatch):
    monkeypatch.setattr(main, 'runs', Runs(main.runs.definitions, store,
                                           execution_enabled=lambda: False, allow_dirty=lambda: False))
    run_id = run(store)
    client = TestClient(main.app)
    listed = client.get('/api/v1/runs').json()
    assert [r['id'] for r in listed] == [run_id] and listed[0]['status'] == 'admitted'
    detail = client.get(f'/api/v1/runs/{run_id}').json()
    assert [e['type'] for e in detail['events']] == ['task.created', 'task.admitted']
    assert client.get('/api/v1/runs/nope').status_code == 404

def test_migration_from_v1_ledger_renames_columns(tmp_path):
    # A ledger created before token budgets (user_version 1) upgrades in place.
    import maef_api.ledger as st
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    for statement in st._split(st.MIGRATIONS[0]):
        conn.execute(statement)
    conn.execute('PRAGMA user_version=1'); conn.commit(); conn.close()
    s = Ledger(path)
    s.ensure_account('default', 10)
    assert s.account('default')['limit_tokens'] == 10
