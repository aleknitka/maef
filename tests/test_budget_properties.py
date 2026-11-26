"""Stateful property test of the budget ledger (SQLite).

Random sequences of admit / claim / usage / finish / cancel / expiry / heartbeat are run against a real
Ledger, and after every step the account and run invariants must hold: reserved equals charged plus
released, the account never exceeds its cap, every event sequence is contiguous, and every run settles once.

Two variants share the machine:
- well-behaved workers (the harness guard keeps reports within the reservation): must pass;
- over-reporting workers (tokens_used and usage above the reservation): must pass too (R1 fix,
  docs/PLAN_OF_ACTION.md 1.1): the ledger charges at most the reservation.
"""

import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from maef_api.definitions import PinnedDefinition
from maef_api.leases import FencingError, SequenceGap
from maef_api.ledger import AdmissionError, AlreadyTerminal, Ledger
from maef_api.models import AgentDefinition, Budget, ModelEndpointProfile
from test_api import PROFILE

CAP = 20_000


def pinned(max_out):
    agent = AgentDefinition(
        schema_version="maef.dev/v1alpha1", id="agent-a", version="0.1.0", display_name="R", description="d"
    )
    profile = ModelEndpointProfile.model_validate(
        yaml.safe_load(PROFILE.format(id="model-m", extra=f"max_output_tokens: {max_out}\n"))
    )
    return PinnedDefinition(
        agent=agent,
        profile=profile,
        instructions="s",
        git_rev="abc",
        dirty=False,
        definition_digest="d",
        profile_digest="p",
    )


class BudgetMachine(RuleBasedStateMachine):
    overrun = False  # True: workers may report above their reservation

    @initialize()
    def setup(self):
        self.dir = Path(tempfile.mkdtemp(prefix="prop-"))
        self.led = Ledger(self.dir / "l.db")
        self.led.ensure_account("acct", CAP)
        self.now = 1000.0
        self.runs = []  # run ids
        self.lease = {}  # run_id -> [Lease, next_wseq]
        self.reported = {}  # run_id -> sum of usage.recorded we sent
        self.final = {}  # run_id -> tokens passed to finish, or "expired" / 0 when cancelled
        self.last_seq = {}

    def teardown(self):
        shutil.rmtree(getattr(self, "dir", None), ignore_errors=True)

    @rule(max_out=st.integers(1, 800), pb=st.integers(0, 3000), turns=st.integers(1, 3))
    def admit(self, max_out, pb, turns):
        try:
            rid = self.led.admit(
                pinned(max_out),
                Budget(max_tokens=10_000, max_turns=turns, max_duration_seconds=60),
                prompt_bytes=pb,
                instruction="q",
                account_id="acct",
            )
        except AdmissionError:
            return
        self.runs.append(rid)
        self.reported[rid] = 0

    @rule()
    def claim(self):
        lease = self.led.claim("w1", 30, self.now)
        if lease:
            self.lease[lease.run_id] = [lease, 1]

    @rule(data=st.data(), n=st.integers(0, 5000), replay=st.booleans())
    def usage(self, data, n, replay):
        live = [r for r in self.lease if r not in self.final]
        if not live:
            return
        rid = data.draw(st.sampled_from(live))
        lease, wseq = self.lease[rid]
        if not self.overrun:  # a well-behaved worker never records more than it reserved
            n = max(0, min(n, self.led.get(rid).reserved_tokens - self.reported[rid]))
        try:
            self.led.report(rid, lease.fencing_token, wseq, "usage.recorded", {"total_tokens": n})
        except (FencingError, SequenceGap):
            return
        self.reported[rid] += n
        self.lease[rid][1] = wseq + 1
        if replay:  # an idempotent replay of the same wseq must not double count
            self.led.report(rid, lease.fencing_token, wseq, "usage.recorded", {"total_tokens": n})

    @rule(data=st.data(), claimed=st.integers(0, 5000), twice=st.booleans())
    def finish(self, data, claimed, twice):
        live = [r for r in self.lease if r not in self.final]
        if not live:
            return
        rid = data.draw(st.sampled_from(live))
        lease, _ = self.lease[rid]
        if not self.overrun:
            claimed = min(claimed, self.led.get(rid).reserved_tokens)
        try:
            self.led.finish(rid, lease.fencing_token, tokens_used=claimed, status="succeeded", terminal_reason="done")
        except FencingError:
            return
        self.final[rid] = claimed
        if twice:
            with pytest.raises(FencingError):
                self.led.finish(
                    rid, lease.fencing_token, tokens_used=claimed, status="succeeded", terminal_reason="done"
                )

    @rule(data=st.data())
    def cancel(self, data):
        if not self.runs:
            return
        rid = data.draw(st.sampled_from(self.runs))
        try:
            outcome = self.led.request_cancel(rid)
        except AlreadyTerminal:
            return
        if outcome == "cancelled":
            self.final[rid] = 0

    @rule(dt=st.integers(0, 100))
    def tick_and_expire(self, dt):
        self.now += dt
        self.led.expire_leases(self.now)
        for rid in self.runs:
            if self.led.status(rid) in ("succeeded", "failed", "cancelled") and rid not in self.final:
                self.final[rid] = "expired"

    @rule(data=st.data())
    def heartbeat(self, data):
        live = [r for r in self.lease if r not in self.final]
        if not live:
            return
        rid = data.draw(st.sampled_from(live))
        with contextlib.suppress(FencingError):
            self.led.heartbeat(rid, self.lease[rid][0].fencing_token, 30, self.now)

    @invariant()
    def invariants(self):
        if not hasattr(self, "led"):
            return
        acct = self.led.account("acct")
        assert acct["reserved_tokens"] >= 0 and acct["spent_tokens"] >= 0
        open_reserved = charged = 0
        with self.led.connect() as conn:
            res = {row["run_id"]: dict(row) for row in conn.execute("SELECT * FROM reservations")}
        for rid in self.runs:
            detail = self.led.get(rid)
            r = res[rid]
            if detail.status in ("succeeded", "failed", "cancelled"):
                assert r["settled_tokens"] is not None, f"terminal run {rid} has open reservation"
                assert r["reserved_tokens"] == r["settled_tokens"] + r["released_tokens"], r
                assert detail.tokens_used == r["settled_tokens"]
                charged += r["settled_tokens"]
                recorded = sum(e.payload.get("total_tokens", 0) for e in detail.events if e.type == "usage.recorded")
                cap = r["reserved_tokens"]
                assert r["settled_tokens"] >= min(recorded, cap), f"charged {r['settled_tokens']} < recorded {recorded}"
                assert r["settled_tokens"] <= cap, "charged above the reservation"
                fin = self.final.get(rid)
                if isinstance(fin, int):
                    assert r["settled_tokens"] == min(max(fin, recorded), cap)
                if fin == "expired":  # charged the recorded usage, or the whole reservation when none was recorded
                    assert r["settled_tokens"] == (min(recorded, cap) if recorded else cap)
                settled_events = [
                    e for e in detail.events if e.type in ("task.succeeded", "task.failed", "task.cancelled")
                ]
                assert len(settled_events) == 1, "double settlement event"
            else:
                assert r["settled_tokens"] is None
                open_reserved += r["reserved_tokens"]
            seqs = [e.seq for e in detail.events]
            assert seqs == list(range(1, len(seqs) + 1)), seqs
            assert len(seqs) >= self.last_seq.get(rid, 0), "events shrank"
            self.last_seq[rid] = len(seqs)
        assert acct["reserved_tokens"] == open_reserved
        assert acct["spent_tokens"] == charged
        assert acct["reserved_tokens"] + acct["spent_tokens"] <= CAP, f"over cap: {acct}"


class OverReportingMachine(BudgetMachine):
    overrun = True


PROPERTY_SETTINGS = settings(
    max_examples=100,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

TestBudgetWellBehaved = BudgetMachine.TestCase
TestBudgetWellBehaved.settings = PROPERTY_SETTINGS


class TestBudgetOverReporting(OverReportingMachine.TestCase):
    settings = PROPERTY_SETTINGS
