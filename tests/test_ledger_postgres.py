"""Postgres-only behaviour: the durable queue under real concurrency, schema versioning, truncate guard."""
import threading

import pytest
from maef_api.ledger import Ledger
from maef_api.models import Budget
from test_ledger import definition

pytestmark = pytest.mark.pg


@pytest.fixture
def url(fresh_postgres_url):
    return fresh_postgres_url()


def _admit(ledger):
    return ledger.admit(definition(), Budget(max_tokens=1000, max_turns=1), prompt_bytes=10, instruction='Q')


def _parallel(n, fn):
    barrier, out = threading.Barrier(n), [None] * n

    def go(i):
        barrier.wait()
        out[i] = fn(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


def test_schema_version_and_reopen(url):
    Ledger(url)
    ledger = Ledger(url)  # second open is a no-op migration
    with ledger.connect() as conn:
        assert conn.execute('SELECT version FROM schema_version').fetchall() == [{'version': 1}]


def test_concurrent_first_start_migrates_once(url):
    errors = [e for e in _parallel(4, lambda i: _try(lambda: Ledger(url))) if e]
    assert errors == []


def _try(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return e


def test_four_workers_claim_three_runs_exactly_once(url):
    ledger = Ledger(url)
    ledger.ensure_account('default', 1_000_000)
    runs = {_admit(ledger) for _ in range(3)}
    claims = _parallel(4, lambda i: Ledger(url).claim(f'w{i}', 60, 1000.0))
    got = [c.run_id for c in claims if c is not None]
    assert sorted(got) == sorted(runs) and sum(c is None for c in claims) == 1
    assert {ledger.get(r).status for r in runs} == {'leased'}


def test_concurrent_events_get_gapless_seq(url):
    ledger = Ledger(url)
    ledger.ensure_account('default', 1_000_000)
    run_id = _admit(ledger)
    _parallel(4, lambda i: [Ledger(url).append(run_id, 'note', {'i': i, 'n': n}) for n in range(5)])
    assert [e.seq for e in ledger.get(run_id).events] == list(range(1, 2 + 20 + 1))


def test_truncate_of_append_only_tables_is_rejected(url):
    ledger = Ledger(url)
    ledger.audit('p', 'a', 't', 'ok', 'c')
    with ledger.connect() as conn:
        for table in ('run_events', 'audit_log'):
            with pytest.raises(ledger.database_error, match='append-only'):
                conn.execute(f'TRUNCATE {table}')
