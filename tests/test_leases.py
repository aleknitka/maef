import pytest
from maef_api.channel import LocalChannel
from maef_api.leases import FencingError, SequenceGap
from maef_api.ledger import Ledger
from maef_api.models import Budget
from test_run_execution import definition

BUDGET = Budget(max_tokens=400_000, max_turns=1)


@pytest.fixture
def store(ledger_target):
    s = Ledger(ledger_target("leases.db"))
    s.ensure_account("default", 1_000_000)
    return s


def admit(store, files=None, instruction="Question?"):
    d = definition()
    if files is not None:
        d = d.model_copy(update={"files": files})
    return store.admit(d, BUDGET, prompt_bytes=105, instruction=instruction)


def channel_at(store, now):
    return LocalChannel(store, lease_seconds=60, clock=lambda: now[0])


def types(store, run_id):
    return [e.type for e in store.get(run_id).events]


def test_claim_on_empty_queue_returns_none(store):
    assert LocalChannel(store).claim("w1") is None


def test_claim_returns_oldest_admitted_run_first(store):
    first, second = admit(store), admit(store)
    channel = LocalChannel(store)
    lease = channel.claim("w1")
    assert lease.run_id == first
    assert store.get(first).status == "leased"
    assert channel.claim("w2").run_id == second
    assert channel.claim("w3") is None


def test_wrong_token_is_refused_everywhere(store):
    now = [1000.0]
    channel = channel_at(store, now)
    run_id = admit(store)
    lease = channel.claim("w1")
    stale = lease.model_copy(update={"fencing_token": lease.fencing_token + 5})
    with pytest.raises(FencingError):
        channel.heartbeat(stale)
    with pytest.raises(FencingError):
        channel.report(stale, 1, "model.request")
    with pytest.raises(FencingError):
        channel.finish(stale, tokens_used=0, status="failed", terminal_reason="x")
    assert types(store, run_id) == ["task.created", "task.admitted", "lease.granted"]


def test_expired_lease_is_swept_and_fenced(store):
    now = [1000.0]
    channel = channel_at(store, now)
    run_id = admit(store)
    lease = channel.claim("w1")  # expires at 1060
    assert store.expire_leases(now=1059.0) == 0
    assert store.expire_leases(now=1061.0) == 1
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason) == ("failed", "worker_lost")
    assert detail.tokens_used == detail.reserved_tokens
    assert store.account("default")["spent_tokens"] == detail.reserved_tokens
    assert "lease.expired" in types(store, run_id) and types(store, run_id)[-1] == "task.failed"
    now[0] = 1100.0
    with pytest.raises(FencingError):
        channel.heartbeat(lease)
    with pytest.raises(FencingError):
        channel.report(lease, 1, "model.request")


def test_late_heartbeat_after_expiry_is_refused_before_sweep(store):
    now = [1000.0]
    channel = channel_at(store, now)
    admit(store)
    lease = channel.claim("w1")
    now[0] = 1030.0
    channel.heartbeat(lease)  # renews to 1090
    now[0] = 1091.0
    with pytest.raises(FencingError):
        channel.heartbeat(lease)


def test_report_is_idempotent_per_wseq(store):
    channel = LocalChannel(store)
    run_id = admit(store)
    lease = channel.claim("w1")
    assert channel.report(lease, 1, "model.request", {"turn": 1}) is False  # returns cancel_requested
    assert channel.report(lease, 1, "model.request", {"turn": 1}) is False  # duplicate wseq is ignored
    assert types(store, run_id).count("model.request") == 1


def test_wseq_gap_is_refused(store):
    channel = LocalChannel(store)
    run_id = admit(store)
    lease = channel.claim("w1")
    channel.report(lease, 1, "model.request")
    with pytest.raises(SequenceGap):
        channel.report(lease, 3, "model.response")
    assert "model.response" not in types(store, run_id)


def test_reserved_event_types_are_refused(store):
    channel = LocalChannel(store)
    admit(store)
    lease = channel.claim("w1")
    with pytest.raises(ValueError):
        channel.report(lease, 1, "task.succeeded")
    with pytest.raises(ValueError):
        channel.report(lease, 1, "lease.granted")


def test_finish_charges_at_least_the_reported_usage(store):
    channel = LocalChannel(store)
    run_id = admit(store)
    lease = channel.claim("w1")
    channel.report(lease, 1, "usage.recorded", {"total_tokens": 200})
    channel.report(lease, 2, "usage.recorded", {"total_tokens": 100})
    channel.finish(lease, tokens_used=10, status="succeeded", terminal_reason="completed")
    detail = store.get(run_id)
    assert (detail.status, detail.tokens_used) == ("succeeded", 300)
    assert store.account("default")["spent_tokens"] == 300


def test_second_finish_is_fenced(store):
    channel = LocalChannel(store)
    admit(store)
    lease = channel.claim("w1")
    channel.finish(lease, tokens_used=1, status="succeeded", terminal_reason="completed")
    with pytest.raises(FencingError):
        channel.finish(lease, tokens_used=1, status="succeeded", terminal_reason="completed")


def test_stored_task_round_trips_non_utf8_files(store):
    files = {"SYSTEM.md": b"Be brief.", "notes/raw.bin": b"\xff\xfe\x00\x80"}
    admit(store, files=files)
    lease = LocalChannel(store).claim("w1")
    assert lease.task.pinned.files == files
