"""Plan 1.2 and 1.4: the worker rides out control-plane outages, and runs end with a classified reason."""

import ast
import re
from pathlib import Path

import pytest

from maef_api import endings
from maef_api.adapters.base import ModelResponse
from maef_api.artifacts import ArtifactStoreError, LocalArtifactStore
from maef_api.channel import LocalChannel
from maef_api.definitions import compute_definition_digest
from maef_api.leases import ChannelError, FencingError
from maef_api.ledger import Ledger
from maef_api.models import Budget, RunRecord
from maef_api.worker import SAFETY_MARGIN_SECONDS, Worker
from test_run_execution import definition

LIMIT = 1_000_000


class FakeTime:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class OkAdapter:
    def converse(self, **kwargs):
        return ModelResponse(text="answer", tool_calls=(), stop_reason="end_turn", input_tokens=7, output_tokens=5)


class DownAdapter:
    def __init__(self, error):
        self.error = error

    def converse(self, **kwargs):
        raise self.error


class BrokenChannel(LocalChannel):
    """Every report raises `error` (forever), counting the attempts."""

    def __init__(self, ledger, error):
        super().__init__(ledger)
        self.error, self.attempts = error, 0

    def report(self, lease, wseq, type, payload=None, status=None):
        self.attempts += 1
        raise self.error


@pytest.fixture
def store(tmp_path):
    ledger = Ledger(tmp_path / "outage.db")
    ledger.ensure_account("default", LIMIT)
    return ledger


def admit(store):
    d = definition()
    d = d.model_copy(update={"definition_digest": compute_definition_digest(d.definition_bytes, d.files)})
    return store.admit(d, Budget(max_tokens=500_000, max_turns=1), prompt_bytes=28, instruction="Question?")


def worker(channel, ft, adapter=None):
    return Worker(
        channel,
        lambda profile: adapter or OkAdapter(),
        worker_id="w1",
        sleep=ft.sleep,
        lease_clock=ft.now,
    )


def test_a_fenced_response_stops_retries_immediately(store):
    run_id = admit(store)
    ft, channel = FakeTime(), BrokenChannel(store, FencingError("lease expired"))
    worker(channel, ft).run_once()
    assert channel.attempts == 1 and ft.sleeps == []
    assert store.get(run_id).status != "succeeded"


def test_a_4xx_that_says_the_run_is_not_ours_stops_retries_immediately(store):
    admit(store)
    ft, channel = FakeTime(), BrokenChannel(store, ChannelError("http_error (HTTP 404)", status=404))
    worker(channel, ft).run_once()
    assert channel.attempts == 1 and ft.sleeps == []


@pytest.mark.parametrize("status", [429, 500, 503, None])
def test_a_retryable_answer_is_retried_until_the_lease_deadline(store, status):
    run_id = admit(store)
    ft, channel = FakeTime(), BrokenChannel(store, ChannelError("down", status=status))
    worker(channel, ft).run_once()
    assert channel.attempts > 5
    # It gave up only when the lease (60 s) less the safety margin was spent, never earlier or later.
    assert 60 - SAFETY_MARGIN_SECONDS - 0.5 <= ft.t <= 60 - SAFETY_MARGIN_SECONDS + 0.001
    assert max(ft.sleeps) <= 8.0 and ft.sleeps[1] > ft.sleeps[0] * 0.9  # capped, and growing at first
    assert store.get(run_id).status == "leased"  # the sweep, not the worker, settles it


def test_provider_outage_ends_as_provider_error(store):
    run_id = admit(store)
    worker(LocalChannel(store), FakeTime(), DownAdapter(TimeoutError("secret"))).run_once()
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason, detail.ending) == ("failed", "provider_error", "infrastructure")
    error = next(e.payload for e in detail.events if e.type == "model.error")
    assert error == {"turn": 1, "code": "model_error:TimeoutError"}


def test_enospc_in_the_artifact_store_ends_the_run_as_artifact_error(store, tmp_path, monkeypatch):
    run_id = admit(store)
    artifacts = LocalArtifactStore(tmp_path / "art")

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(artifacts, "put", full)
    channel = LocalChannel(store, artifacts=artifacts)
    assert worker(channel, FakeTime()).run_once() is True  # the worker loop survives
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason, detail.ending) == ("failed", "artifact_error", "infrastructure")


def test_artifact_store_error_is_not_retried():
    assert not issubclass(ArtifactStoreError, ChannelError)


# --- 1.4: every terminal reason is classified exactly once ---


def _reasons_in_code() -> set[str]:
    """Reason strings in apps/api: the `reason`/`terminal_reason`/`stop` assignments and calls, by base."""
    found: set[str] = set()
    root = Path(__file__).resolve().parents[1] / "apps" / "api" / "maef_api"
    for path in root.rglob("*.py"):
        if path.name in ("endings.py", "egress_proxy.py"):  # the proxy logs deny reasons, not run endings
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            values: list[ast.expr] = []
            if isinstance(node, ast.Assign) and any(
                (isinstance(t, ast.Name) and t.id == "reason") or isinstance(t, ast.Tuple) for t in node.targets
            ):
                if isinstance(node.value, ast.Tuple):
                    targets = node.targets[0]
                    if isinstance(targets, ast.Tuple):
                        for t, v in zip(targets.elts, node.value.elts, strict=False):
                            if isinstance(t, ast.Name) and t.id == "reason":
                                values.append(v)
                else:
                    values.append(node.value)
            if isinstance(node, ast.Call):
                values += [kw.value for kw in node.keywords if kw.arg == "terminal_reason"]
                if isinstance(node.func, ast.Name) and node.func.id == "reject" and node.args:
                    values.append(node.args[0])
                if isinstance(node.func, ast.Name) and node.func.id == "_StartupFailure" and node.args:
                    values.append(node.args[0])
                if isinstance(node.func, ast.Name) and node.func.id == "_settle" and len(node.args) > 5:
                    values.append(node.args[5])
                if isinstance(node.func, ast.Attribute) and node.func.attr == "release" and len(node.args) > 1:
                    values.append(node.args[1])
            if isinstance(node, ast.Return) and node.value is not None and path.name == "guard.py":
                values.append(node.value)  # the guard's stop reasons are returned, then become the terminal reason
            for v in values:
                text = None
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    text = v.value
                elif isinstance(v, ast.JoinedStr) and v.values and isinstance(v.values[0], ast.Constant):
                    text = str(v.values[0].value)
                if text and re.fullmatch(r"[a-z_]+:?", text):
                    found.add(text.rstrip(":"))
    return found


def test_every_terminal_reason_used_in_code_is_mapped():
    used = _reasons_in_code() - {"failed", "succeeded", "cancelled"}
    assert {"completed", "provider_error", "worker_lost", "tokens_exhausted", "setup_error"} <= used, used
    unmapped = {r for r in used if r not in endings.TERMINAL_REASONS}
    assert not unmapped, f"terminal reasons in code but not in endings.py: {sorted(unmapped)}"


def test_the_table_has_no_reason_in_both_classes():
    assert not endings.AGENT_REASONS & endings.INFRASTRUCTURE_REASONS


@pytest.mark.parametrize(
    ("reason", "ending"),
    [
        ("completed", "agent"),
        ("tokens_exhausted", "agent"),
        ("cancelled_by_user", "agent"),
        ("unsupported_stop_reason:tool_x", "agent"),
        ("worker_lost", "infrastructure"),
        ("provider_error", "infrastructure"),
        ("setup_error:RuntimeError", "infrastructure"),
        ("credential_missing:FETCH_TOKEN", "infrastructure"),
        ("something_new", "infrastructure"),
        (None, None),
    ],
)
def test_ending_of(reason, ending):
    assert endings.ending_of(reason) == ending


def test_run_record_exposes_the_ending(store):
    run_id = admit(store)
    assert store.get(run_id).ending is None
    store.expire_leases(now=10**12)  # nothing leased: still queued
    lease = store.claim("w1", 30, now=1_000.0)
    assert lease is not None
    store.expire_leases(now=1_031.0)
    record = store.get(run_id)
    assert record.terminal_reason == "worker_lost" and record.ending == "infrastructure"
    assert RunRecord.model_validate(record.model_dump(exclude={"ending", "events"})).ending == "infrastructure"
    assert record.model_dump()["ending"] == "infrastructure"
