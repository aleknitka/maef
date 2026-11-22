import sqlite3
import threading
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from maef_api import main
from maef_api.admission import AdmissionError, admit, release, settle
from maef_api.models import Budget, PriceSchedule, ResolvedDefinition
from maef_api.store import Store

def definition(priced=True, dirty=False):
    price = PriceSchedule(schema_version='maef.dev/v1alpha1', id='test-prices', effective_from=date(2026, 1, 1),
                          input_usd_per_million_tokens=Decimal('1'), output_usd_per_million_tokens=Decimal('5')) if priced else None
    return ResolvedDefinition(agent_id='research-analyst', agent_version='0.1.0', git_rev='abc', dirty=dirty,
                              definition_digest='d', model_profile_id='m', profile_digest='p', price_schedule=price)

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'ledger.db')
    s.ensure_account('default', 1_000_000)  # $1.00
    return s

def run(store, budget=None, **kw):
    kw.setdefault('definition', definition())
    return admit(store, kw.pop('definition'), budget or Budget(max_cost_usd=Decimal('0.4'), max_turns=1),
                 instruction='hello', system_prompt_bytes=100, max_output_tokens=1000, **kw)

def test_admit_reserves_and_logs_events(store):
    run_id = run(store)
    detail = store.get_run(run_id)
    assert [e['type'] for e in detail['events']] == ['task.created', 'task.admitted']
    assert [e['seq'] for e in detail['events']] == [1, 2]
    # worst case is far below the $0.40 cap: 105 in-tokens + 1000 out-tokens
    assert detail['reserved_micro'] == 5105  # 105 in-tokens * $1/M + 1000 out-tokens * $5/M
    assert store.account('default')['reserved_micro'] == 5105

def test_unpriced_profile_rejected(store):
    with pytest.raises(AdmissionError) as e:
        run(store, definition=definition(priced=False))
    assert e.value.code == 'unpriced' and store.list_runs() == []

def test_dirty_refused_unless_flagged(store):
    with pytest.raises(AdmissionError) as e:
        run(store, definition=definition(dirty=True))
    assert e.value.code == 'dirty'
    run_id = run(store, definition=definition(dirty=True), allow_dirty=True)
    assert store.get_run(run_id)['events'][0]['payload']['dirty_allowed'] is True

def test_settle_releases_unused(store):
    run_id = run(store, budget=Budget(max_cost_usd=Decimal('0.4'), max_turns=100))
    reserved = store.get_run(run_id)['reserved_micro']
    assert reserved > 100
    settle(store, run_id, cost_micro=100, status='succeeded', terminal_reason='completed')
    acct = store.account('default')
    assert (acct['reserved_micro'], acct['spent_micro']) == (0, 100)
    detail = store.get_run(run_id)
    assert detail['status'] == 'succeeded' and detail['events'][-1]['payload']['released_micro_usd'] == reserved - 100
    with pytest.raises(AdmissionError):
        settle(store, run_id, cost_micro=1, status='succeeded', terminal_reason='again')

def test_release_returns_everything(store):
    run_id = run(store)
    release(store, run_id, 'cancelled_before_start')
    acct = store.account('default')
    assert (acct['reserved_micro'], acct['spent_micro']) == (0, 0)

def test_concurrent_admission_exactly_one_fits(tmp_path):
    store = Store(tmp_path / 'c.db')
    store.ensure_account('default', 500_000)  # fits one $0.40 reservation, not two
    # large max_turns/outputs push the worst case above the cap, so each reserves $0.40
    budget = Budget(max_cost_usd=Decimal('0.4'), max_turns=100)
    results, barrier = [], threading.Barrier(4)
    def worker():
        barrier.wait()
        try:
            admit(store, definition(), budget, instruction='x', system_prompt_bytes=10, max_output_tokens=100_000)
            results.append('ok')
        except AdmissionError as e:
            results.append(e.code)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(results) == ['insufficient_budget'] * 3 + ['ok']
    assert len(store.list_runs()) == 1

def test_events_are_append_only(store):
    run_id = run(store)
    with store.connect() as conn:
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            conn.execute("UPDATE run_events SET type='x' WHERE run_id=?", (run_id,))
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            conn.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))

def test_failed_admission_leaves_no_partial_state(store):
    with pytest.raises(AdmissionError):
        run(store, budget=Budget(max_cost_usd=Decimal('5'), max_turns=100), account_id='missing')
    assert store.list_runs() == []

def test_runs_api(store, monkeypatch):
    monkeypatch.setattr(main, 'store', store)
    run_id = run(store)
    client = TestClient(main.app)
    listed = client.get('/api/v1/runs').json()
    assert [r['id'] for r in listed] == [run_id] and listed[0]['status'] == 'admitted'
    detail = client.get(f'/api/v1/runs/{run_id}').json()
    assert [e['type'] for e in detail['events']] == ['task.created', 'task.admitted']
    assert client.get('/api/v1/runs/nope').status_code == 404
