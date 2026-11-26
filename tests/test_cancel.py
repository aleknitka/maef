"""Cancellation: the ledger and HTTP route, the worker and harness between steps, budget guard,
run_command, the OCI exec kill path and the HttpChannel flag parsing."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from maef_api import main
from maef_api.adapters.base import ModelResponse, ToolCall, ToolSpec
from maef_api.channel import HttpChannel, LocalChannel
from maef_api.credentials import Secret
from maef_api.definitions import compute_definition_digest
from maef_api.guard import BudgetGuard
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.leases import ChannelError, Lease, TaskPayload
from maef_api.ledger import AlreadyTerminal, Ledger, UnknownRun
from maef_api.models import Budget, SandboxProfile
from maef_api.runs import Runs
from maef_api.sandbox import ExecResult, FakeSession, OciCliSandbox
from maef_api.tools import BuiltinTools, ToolResult
from maef_api.worker import Worker
from test_run_execution import definition

BUDGET = Budget(max_tokens=500_000, max_turns=2)
PINNED_IMAGE = "docker.io/library/busybox@sha256:" + "a" * 64


@pytest.fixture
def store(ledger_target):
    s = Ledger(ledger_target("cancel.db"))
    s.ensure_account("default", 1_000_000)
    return s


def pinned(tools=()):
    d = definition()
    d = d.model_copy(update={"agent": d.agent.model_copy(update={"tools": list(tools)})})
    return d.model_copy(
        update={"definition_digest": compute_definition_digest(d.definition_bytes, d.files, d.mcp_bytes)}
    )


def admit(store, tools=()):
    return store.admit(pinned(tools), BUDGET, prompt_bytes=28, instruction="Question?")


def events(store, run_id):
    return [e.type for e in store.get(run_id).events]


def reserved_and_spent(store):
    acct = store.account("default")
    return acct["reserved_tokens"], acct["spent_tokens"]


# --- Ledger: request_cancel ---


def test_cancel_admitted_run_settles_now_and_releases_reservation(store):
    run = admit(store)
    assert reserved_and_spent(store)[0] > 0
    assert store.request_cancel(run) == "cancelled"
    detail = store.get(run)
    assert (detail.status, detail.terminal_reason) == ("cancelled", "cancelled_by_user")
    assert reserved_and_spent(store) == (0, 0)
    assert "cancel.requested" in events(store, run) and events(store, run)[-1] == "task.cancelled"
    assert LocalChannel(store).claim("w1") is None


def test_cancel_leased_run_is_requested_once(store):
    run = admit(store)
    channel = LocalChannel(store)
    lease = channel.claim("w1")
    assert store.request_cancel(run) == "requested"
    assert store.request_cancel(run) == "requested"
    assert events(store, run).count("cancel.requested") == 1
    assert store.get(run).status == "leased"
    assert channel.heartbeat(lease) is True
    assert channel.report(lease, 1, "model.request", {}) is True


def test_cancel_terminal_run_raises_already_terminal(store):
    run = admit(store)
    store.settle(run, tokens_used=0, status="succeeded", terminal_reason="completed")
    with pytest.raises(AlreadyTerminal):
        store.request_cancel(run)


def test_cancel_unknown_run_raises(store):
    with pytest.raises(UnknownRun):
        store.request_cancel("no-such-run")


# --- HTTP: POST /api/v1/runs/{id}/cancel ---


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr(
        main, "runs", Runs(main.runs.definitions, store, execution_enabled=lambda: True, allow_dirty=lambda: True)
    )
    return TestClient(main.app)


def test_cancel_route_outcomes(store, api):
    leased, admitted = admit(store), admit(store)
    LocalChannel(store).claim("w1")  # oldest first: `leased` is now held by a worker
    assert api.post(f"/api/v1/runs/{leased}/cancel").json() == {"outcome": "requested"}
    assert api.post(f"/api/v1/runs/{leased}/cancel").status_code == 202
    res = api.post(f"/api/v1/runs/{admitted}/cancel")
    assert res.status_code == 202 and res.json() == {"outcome": "cancelled"}
    res = api.post(f"/api/v1/runs/{admitted}/cancel")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "already_terminal"
    assert api.post("/api/v1/runs/no-such-run/cancel").status_code == 404


# --- Worker and harness: cancel between steps ---


class _Replies:
    def __init__(self, *replies):
        self.replies = list(replies)

    def converse(self, **kwargs):
        return self.replies.pop(0)


class CancelDuringFirstCall:
    """Requests cancel through the ledger while the first model call is in flight, then asks for a tool."""

    def __init__(self, store, run_id):
        self.store, self.run_id, self.calls = store, run_id, 0

    def converse(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.store.request_cancel(self.run_id)
            return ModelResponse("", (ToolCall("c1", "current_time", {}),), "tool_use", 7, 5)
        return ModelResponse("late answer", (), "end_turn", 7, 5)


def test_worker_cancel_mid_run_skips_tool_and_next_turn(store):
    run = admit(store, tools=["current_time"])
    adapter = CancelDuringFirstCall(store, run)
    assert Worker(LocalChannel(store), lambda p: adapter, worker_id="w1").run_once() is True
    detail = store.get(run)
    assert (detail.status, detail.terminal_reason, detail.tokens_used) == ("cancelled", "cancelled_by_user", 12)
    assert adapter.calls == 1
    assert "tool.call" not in events(store, run)
    assert reserved_and_spent(store) == (0, 12)


class TwoCallsInOneTurn:
    def __init__(self, store, run_id):
        self.store, self.run_id, self.invoked = store, run_id, []

    def specs(self):
        return [ToolSpec("step", "One step.", {"type": "object", "properties": {"n": {"type": "integer"}}})]

    def invoke(self, name, arguments):
        self.invoked.append(arguments["n"])
        if len(self.invoked) == 1:
            self.store.request_cancel(self.run_id)
        return ToolResult("ok")


def test_cancel_inside_a_tool_skips_the_rest_of_the_turn(store):
    run = admit(store)
    tools = TwoCallsInOneTurn(store, run)
    adapter = _Replies(
        ModelResponse("", (ToolCall("a", "step", {"n": 1}), ToolCall("b", "step", {"n": 2})), "tool_use", 7, 5),
        ModelResponse("never", (), "end_turn", 7, 5),
    )
    reserved = store.get(run).reserved_tokens
    prepared = PreparedRun(
        run, pinned(), BUDGET, reserved, "Question?", tools=tools, cancelled=lambda: store.get(run).cancel_requested
    )
    execute(LedgerSink(store, run), prepared, adapter)
    assert tools.invoked == [1]
    assert events(store, run).count("tool.call") == 1
    detail = store.get(run)
    assert (detail.status, detail.terminal_reason) == ("cancelled", "cancelled_by_user")
    assert len(adapter.replies) == 1  # no second model call


# --- Budget guard, run_command ---


def test_budget_guard_reports_cancel_even_with_limits_fine():
    guard = BudgetGuard(BUDGET, reserved_tokens=10**6, max_output_tokens=10, cancelled=lambda: True)
    assert guard.check(prompt_bytes=1) == "cancelled"
    not_cancelled = BudgetGuard(BUDGET, reserved_tokens=10**6, max_output_tokens=10, cancelled=lambda: False)
    assert not_cancelled.check(prompt_bytes=1) is None


def test_run_command_with_set_cancel_event_is_an_error_marked_cancelled():
    cancel = threading.Event()
    cancel.set()
    session = FakeSession(lambda argv, stdin: ExecResult(0, b"", b""))
    tools = BuiltinTools(["run_command"], {}, session=session, cancel=cancel)
    result = tools.invoke("run_command", {"argv": ["ls"]})
    assert result.is_error is True
    assert json.loads(result.content)["cancelled"] is True


# --- OCI CLI exec: a sleeping exec is killed when the event is set ---


@pytest.fixture
def fake_podman(tmp_path):
    log = tmp_path / "podman.calls"
    path = tmp_path / "podman"
    path.write_text(
        "#!/bin/sh\n"
        "{ printf '%s\\0' \"$@\"; printf '\\n'; } >> '" + str(log) + "'\n"
        'case "$*" in\n'
        '  --version*) echo "podman version 5.2.1";;\n'
        "  *Rootless*) echo true;;\n"
        '  *"sleep 30"*) exec sleep 30;;\n'
        "esac\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return path, log


def test_oci_exec_cancel_returns_quickly_and_kills_the_process(fake_podman):
    path, log = fake_podman
    sandbox = OciCliSandbox("podman", str(path), env={})
    profile = SandboxProfile(
        schema_version="maef.dev/v1alpha1",
        id="restricted-local",
        image=PINNED_IMAGE,
        cpus=1,
        memory_mb=64,
        pids_limit=32,
        workspace_mb=8,
    )
    session = sandbox.provision(profile, "run-cancel")
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    result = session.exec(["sleep", "30"], timeout=20, cancel=cancel)
    assert time.monotonic() - started < 5
    assert (result.cancelled, result.timed_out) == (True, False)
    calls = [[a.decode() for a in line.split(b"\0")[:-1]] for line in log.read_bytes().split(b"\n") if line]
    assert ["exec", session.name, "sh", "-c", "kill -9 -1"] in calls
    session.teardown()


# --- HttpChannel: cancel_requested from heartbeat and events responses ---


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.paths.append(self.path)
        raw = self.server.reply if isinstance(self.server.reply, bytes) else json.dumps(self.server.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    server.reply, server.paths = {}, []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def http_lease():
    return Lease(
        run_id="run-1",
        fencing_token=1,
        expires_at=0.0,
        task=TaskPayload(instruction="x", budget=BUDGET, reserved_tokens=1, pinned=definition()),
    )


def channel(stub):
    return HttpChannel(f"http://127.0.0.1:{stub.server_address[1]}", Secret("tok"))


def test_heartbeat_parses_cancel_requested(stub, http_lease):
    stub.reply = {"ok": True, "cancel_requested": True}
    assert channel(stub).heartbeat(http_lease) is True
    assert stub.paths[-1] == "/api/v1/worker/runs/run-1/heartbeat"
    stub.reply = {"ok": True, "cancel_requested": False}
    assert channel(stub).heartbeat(http_lease) is False


def test_events_parses_cancel_requested(stub, http_lease):
    stub.reply = {"cancel_requested": True}
    assert channel(stub).report(http_lease, 1, "model.request", {}) is True
    assert stub.paths[-1] == "/api/v1/worker/runs/run-1/events"
    stub.reply = {"cancel_requested": False}
    assert channel(stub).report(http_lease, 2, "model.request", {}) is False


def test_missing_flag_is_not_a_cancel(stub, http_lease):
    stub.reply = {"ok": True}
    assert channel(stub).heartbeat(http_lease) is False


def test_garbled_response_is_a_channel_error(stub, http_lease):
    stub.reply = b"not json"
    with pytest.raises(ChannelError):
        channel(stub).heartbeat(http_lease)
