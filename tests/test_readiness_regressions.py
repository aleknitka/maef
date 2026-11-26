"""Regression tests for readiness findings R1 and R2 (docs/PLAN_OF_ACTION.md phase 1).

Each test is a strict xfail: it fails today, for the reason in its marker, and phase 1 flips it to passing.
A strict xfail that starts passing reports XPASS and fails the run, which forces the marker to be removed.
Run `pytest tests/test_readiness_regressions.py --runxfail` to see the real failure messages.
"""

import pytest

from maef_api import worker as worker_module
from maef_api.adapters.base import ModelResponse
from maef_api.channel import LocalChannel
from maef_api.definitions import compute_definition_digest
from maef_api.leases import ChannelError
from maef_api.ledger import Ledger
from maef_api.models import Budget
from maef_api.worker import Worker
from test_run_execution import definition

LIMIT = 1_000_000
R1 = "R1: finish accepts tokens_used above the reservation and overdraws the account (docs/PLAN_OF_ACTION.md 1.1)"
R2 = (
    "R2: a worker gives up on a run after SEND_ATTEMPTS=3 channel errors (about 3 s of backoff); "
    "a longer control-plane outage abandons the run (worker.py SEND_ATTEMPTS=3, docs/PLAN_OF_ACTION.md 1.2)"
)


class FakeAdapter:
    def converse(self, **kwargs):
        return ModelResponse(text="answer", tool_calls=(), stop_reason="end_turn", input_tokens=7, output_tokens=5)


class OverReportingChannel(LocalChannel):
    """A worker that claims 999,999,999 tokens at finish, far above anything it reserved."""

    def finish(self, lease, *, tokens_used, status, terminal_reason):
        super().finish(lease, tokens_used=999_999_999, status=status, terminal_reason=terminal_reason)


class OutageChannel(LocalChannel):
    """Refuses every report for the first `outage` attempts, as a control plane that is restarting would."""

    def __init__(self, ledger, outage):
        super().__init__(ledger)
        self.remaining, self.refused = outage, 0

    def report(self, lease, wseq, type, payload=None, status=None):
        if self.remaining > 0:
            self.remaining -= 1
            self.refused += 1
            raise ChannelError("connection refused")
        return super().report(lease, wseq, type, payload, status)


@pytest.fixture
def store(tmp_path):
    ledger = Ledger(tmp_path / "regressions.db")
    ledger.ensure_account("default", LIMIT)
    return ledger


def admit(store, max_tokens=500_000):
    d = definition()
    d = d.model_copy(update={"definition_digest": compute_definition_digest(d.definition_bytes, d.files)})
    return store.admit(d, Budget(max_tokens=max_tokens, max_turns=1), prompt_bytes=28, instruction="Question?")


@pytest.fixture
def sleeps(monkeypatch):
    """Records the worker's retry backoff instead of sleeping, so an outage of minutes runs in milliseconds."""
    recorded = []
    real_init = worker_module.ChannelSink.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("sleep", recorded.append)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(worker_module.ChannelSink, "__init__", init)
    return recorded


def test_r1a_overreported_tokens_are_rejected_or_clamped(store):
    run_id = admit(store)
    Worker(OverReportingChannel(store), lambda profile: FakeAdapter(), worker_id="w1").run_once()
    account = store.account("default")
    assert account["reserved_tokens"] + account["spent_tokens"] <= LIMIT, (
        f"account overdrawn: {account}; run {run_id} status {store.get(run_id).status}"
    )


def test_r1b_expired_lease_charges_recorded_usage_above_reservation(store):
    run_id = admit(store)
    reserved = store.get(run_id).reserved_tokens
    recorded = reserved + 1_000
    lease = store.claim("w1", 30, now=1_000.0)
    assert lease is not None and lease.run_id == run_id
    store.report(run_id, lease.fencing_token, 1, "usage.recorded", {"total_tokens": recorded})
    store.expire_leases(now=1_031.0)  # the lease lapsed without a finish
    account = store.account("default")
    # Plan 1.1: charge max(recorded, reported) capped at the reservation, so recorded usage above the
    # reservation is charged as the reservation (the account can never be overdrawn).
    assert account["spent_tokens"] == reserved, (
        f"charged {account['spent_tokens']} (the reservation was {reserved}); worker recorded {recorded}"
    )


@pytest.mark.xfail(strict=True, reason=R2)
def test_r2_worker_rides_out_a_channel_outage_longer_than_three_seconds(store, sleeps):
    run_id = admit(store)
    # 8 refused reports: under the capped 1, 2, 4, 4, ... backoff that is about 23 s of outage, not 3 s.
    channel = OutageChannel(store, outage=8)
    Worker(channel, lambda profile: FakeAdapter(), worker_id="w1").run_once()
    detail = store.get(run_id)
    assert detail.status == "succeeded", (
        f"run {detail.status}/{detail.terminal_reason} after {channel.refused} refused reports, backoff sleeps {sleeps}"
    )
