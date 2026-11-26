"""Token accounting rules (docs/PLAN_OF_ACTION.md 1.1), on SQLite and Postgres.

Charge = min(max(reported, recorded), reservation) on every settle path; the admission check is
`limit - reserved - spent`; an account period can be reset with an audit row.
"""

import pytest

from maef_api import cli
from maef_api.ledger import AdmissionError, Ledger
from maef_api.models import Budget
from test_ledger import definition

RESERVE = 1105  # definition() with prompt_bytes=105: 105 prompt tokens + 1000 output tokens


@pytest.fixture
def store(ledger_target):
    return Ledger(ledger_target("acct.db"))


def admit(store):
    return store.admit(definition(), Budget(max_tokens=400_000, max_turns=1), prompt_bytes=105, instruction="Q")


def leased(store):
    run_id = admit(store)
    lease = store.claim("w1", 30, now=1_000.0)
    assert lease is not None and lease.run_id == run_id
    return run_id, lease


def events(store, run_id):
    return {e.type: e.payload for e in store.get(run_id).events}


# --- admission check: kills the surviving mutants at ledger.py `free = limit - reserved - spent` ---


def test_admission_counts_spent_tokens(store):
    store.ensure_account("default", 2 * RESERVE - 1)
    run_id, lease = leased(store)
    store.finish(run_id, lease.fencing_token, tokens_used=RESERVE, status="succeeded", terminal_reason="done")
    assert store.account("default")["spent_tokens"] == RESERVE
    with pytest.raises(AdmissionError) as err:  # dropping `spent` would see the full limit free
        admit(store)
    assert err.value.code == "insufficient_budget"


def test_admission_counts_reserved_tokens(store):
    store.ensure_account("default", 2 * RESERVE - 1)
    admit(store)
    with pytest.raises(AdmissionError) as err:  # dropping `reserved` would admit a second run
        admit(store)
    assert err.value.code == "insufficient_budget"


def test_admission_boundary_is_inclusive(store):
    store.ensure_account("default", 2 * RESERVE)
    admit(store)
    admit(store)  # needs exactly what is free: `>=` instead of `>` would refuse it
    assert store.account("default")["reserved_tokens"] == 2 * RESERVE
    with pytest.raises(AdmissionError):
        admit(store)


# --- clamp and settle rules ---


def test_overreported_finish_is_clamped_and_audited(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.finish(run_id, lease.fencing_token, tokens_used=999_999_999, status="succeeded", terminal_reason="done")
    detail = store.get(run_id)
    assert detail.status == "succeeded" and detail.tokens_used == RESERVE
    account = store.account("default")
    assert (account["reserved_tokens"], account["spent_tokens"]) == (0, RESERVE)
    ev = events(store, run_id)
    assert ev["budget.overreport"] == {
        "reported_tokens": 999_999_999,
        "recorded_tokens": 0,
        "reserved_tokens": RESERVE,
        "charged_tokens": RESERVE,
    }
    assert ev["task.succeeded"]["tokens_used"] == RESERVE


def test_within_reservation_has_no_overreport_event(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.finish(run_id, lease.fencing_token, tokens_used=RESERVE, status="succeeded", terminal_reason="done")
    assert "budget.overreport" not in events(store, run_id)


def test_finish_charges_the_larger_of_reported_and_recorded(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.report(run_id, lease.fencing_token, 1, "usage.recorded", {"total_tokens": 700})
    store.finish(run_id, lease.fencing_token, tokens_used=100, status="failed", terminal_reason="x")
    assert store.get(run_id).tokens_used == 700  # a worker cannot under-report


def test_workers_cannot_forge_budget_events(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    with pytest.raises(ValueError):
        store.report(run_id, lease.fencing_token, 1, "budget.overreport", {})


def test_expired_lease_charges_recorded_usage(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.report(run_id, lease.fencing_token, 1, "usage.recorded", {"total_tokens": 300})
    store.expire_leases(now=1_031.0)
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason, detail.tokens_used) == ("failed", "worker_lost", 300)
    account = store.account("default")
    assert (account["reserved_tokens"], account["spent_tokens"]) == (0, 300)


def test_expired_lease_without_usage_charges_the_whole_reservation(store):
    store.ensure_account("default", 1_000_000)
    run_id, _ = leased(store)
    store.expire_leases(now=1_031.0)
    assert store.get(run_id).tokens_used == RESERVE
    assert store.account("default")["spent_tokens"] == RESERVE


def test_expired_lease_with_usage_above_reservation_is_capped(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.report(run_id, lease.fencing_token, 1, "usage.recorded", {"total_tokens": RESERVE + 1_000})
    store.expire_leases(now=1_031.0)
    assert store.get(run_id).tokens_used == RESERVE
    assert "budget.overreport" in events(store, run_id)


def test_cancel_charges_recorded_usage(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.report(run_id, lease.fencing_token, 1, "usage.recorded", {"total_tokens": 250})
    store.finish(run_id, lease.fencing_token, tokens_used=0, status="cancelled", terminal_reason="cancelled_by_user")
    assert store.get(run_id).tokens_used == 250


# --- account period reset ---


def test_reset_account_zeroes_spent_and_audits(store):
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.finish(run_id, lease.fencing_token, tokens_used=400, status="succeeded", terminal_reason="done")
    assert store.reset_account("default") == 400
    assert store.account("default")["spent_tokens"] == 0
    rows = [r for r in store.audit_after(0, 100) if r["action"] == "account.reset"]
    assert len(rows) == 1 and rows[0]["target"] == "account:default" and "cleared=400" in rows[0]["outcome"]


def test_reset_refused_while_reserved_unless_forced(store):
    store.ensure_account("default", 1_000_000)
    admit(store)
    with pytest.raises(AdmissionError) as err:
        store.reset_account("default")
    assert err.value.code == "reserved"
    assert [r for r in store.audit_after(0, 100) if r["action"] == "account.reset"] == []
    store.reset_account("default", force=True)
    assert store.account("default")["reserved_tokens"] == RESERVE  # the open run keeps its reservation


def test_reset_unknown_account(store):
    with pytest.raises(AdmissionError) as err:
        store.reset_account("nope")
    assert err.value.code == "no_account"


def test_cli_admin_account_reset(store, monkeypatch, capsys):
    monkeypatch.setenv("MAEF_DB", store.url)
    store.ensure_account("default", 1_000_000)
    run_id, lease = leased(store)
    store.finish(run_id, lease.fencing_token, tokens_used=400, status="succeeded", terminal_reason="done")
    assert cli.main(["admin", "account", "reset"]) == 0
    assert "cleared 400" in capsys.readouterr().err
    assert store.account("default")["spent_tokens"] == 0
    admit(store)
    assert cli.main(["admin", "account", "reset", "default"]) != 0
    assert cli.main(["admin", "account", "reset", "default", "--force"]) == 0
