import json
import threading
from contextlib import contextmanager

import pytest
import yaml
from maef_api import main
from maef_api.adapters.base import ModelResponse
from maef_api.adapters.decision import FakeDecisionAdapter
from maef_api.channel import LocalChannel
from maef_api.cli import main as cli_main
from maef_api.client import MaefClient
from maef_api.definitions import compute_definition_digest
from maef_api.evals import (
    CheckResult,
    SuiteResult,
    TaskResult,
    compare,
    load_suite,
    run_suite,
)
from maef_api.registry import ROOT, ModelProfileRegistry
from maef_api.worker import Worker
from pydantic import ValidationError
from test_decisions import GOOD, JEV_YAML, QUESTIONS
from test_run_execution import definition


@contextmanager
def serving(worker):
    """Run queued runs on a background thread while the suite runs on the calling one."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            if not worker.run_once():
                stop.wait(0.01)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(5)


def agent_worker(live_api):
    return Worker(LocalChannel(live_api.ledger, artifacts=main.artifacts), lambda p: live_api.adapter, worker_id="eval-w")


def write_suite(path, tasks, suite_id="checks"):
    path.write_text(yaml.safe_dump({"schema_version": "maef.dev/v1alpha1", "id": suite_id, "tasks": tasks}))
    return path


def agent_task(task_id, *checks, agent_id="research-analyst"):
    return {"id": task_id, "agent_id": agent_id, "instruction": "Say the answer.", "checks": list(checks)}


# --- suite format ---

def test_smoke_suite_loads():
    suite = load_suite(ROOT / "evals" / "smoke.yaml")
    assert suite.id == "smoke"
    assert [t.id for t in suite.tasks] == ["greet", "arithmetic"]


def test_extra_fields_are_rejected(tmp_path):
    path = write_suite(tmp_path / "s.yaml", [{**agent_task("a", {"status": "succeeded"}), "colour": "red"}])
    with pytest.raises(ValidationError):
        load_suite(path)


def test_unknown_check_type_is_rejected(tmp_path):
    path = write_suite(tmp_path / "s.yaml", [agent_task("a", {"sentiment": "positive"})])
    with pytest.raises(ValidationError):
        load_suite(path)


def test_check_must_set_exactly_one_key(tmp_path):
    path = write_suite(tmp_path / "s.yaml", [agent_task("a", {"contains": "x", "status": "succeeded"})])
    with pytest.raises(ValidationError):
        load_suite(path)


def test_task_ids_must_be_unique(tmp_path):
    path = write_suite(tmp_path / "s.yaml", [agent_task("a", {"status": "succeeded"}),
                                             agent_task("a", {"status": "succeeded"})])
    with pytest.raises(ValidationError):
        load_suite(path)


# --- running checks against real runs ---

CHECKS = {
    "c-contains-pass": ({"contains": "42"}, True),
    "c-contains-fail": ({"contains": "99"}, False),
    "c-not-contains-pass": ({"not_contains": "99"}, True),
    "c-not-contains-fail": ({"not_contains": "42"}, False),
    "c-regex-pass": ({"regex": r"^Answer is \d+"}, True),
    "c-regex-fail": ({"regex": r"^Nope"}, False),
    "c-status-pass": ({"status": "succeeded"}, True),
    "c-status-fail": ({"status": "failed"}, False),
    "c-tokens-pass": ({"max_tokens": 1000}, True),
    "c-tokens-fail": ({"max_tokens": 50}, False),
}


def test_text_status_and_token_checks_pass_and_fail(live_api, tmp_path):
    live_api.adapter.text = "Answer is 42 and FINAL."  # 100 in + 10 out = 110 tokens
    tasks = [agent_task(tid, check) for tid, (check, _) in CHECKS.items()]
    suite = load_suite(write_suite(tmp_path / "checks.yaml", tasks))
    with serving(agent_worker(live_api)):
        result = run_suite(MaefClient(live_api.transport), suite, timeout_seconds=5)
    by_id = {t.task_id: t for t in result.tasks}
    for tid, (_, expected) in CHECKS.items():
        assert by_id[tid].passed is expected, tid
        assert by_id[tid].status == "succeeded"
        assert by_id[tid].tokens_used == 110
    assert result.failed_tasks == [tid for tid, (_, ok) in CHECKS.items() if not ok]


class DecisionAsAgent:
    """run_suite submits through `submit`; a decision task is submitted as a decision run instead. Everything
    else (wait, result_text, answers, cancel) is the real client."""

    def __init__(self, client):
        self._client = client

    def submit(self, agent_id, instruction, budget=None, agent_revision=None):
        return {"run_id": self._client.decide(agent_id, instruction, QUESTIONS)["run_id"]}

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def decision_worker(live_api, tmp_path, monkeypatch):
    profiles = tmp_path / "decision-profiles"
    profiles.mkdir()
    (profiles / "jev-decisions.yaml").write_text(JEV_YAML)
    monkeypatch.setattr(main.runs.definitions, "profiles", ModelProfileRegistry(profiles))
    fake = FakeDecisionAdapter()
    fake.answers = GOOD
    return Worker(LocalChannel(live_api.ledger, artifacts=main.artifacts), lambda p: live_api.adapter,
                  worker_id="eval-dec", decision_factory=lambda p: fake)


def test_json_path_checks_against_decision_answers(live_api, decision_worker, tmp_path):
    tasks = [
        agent_task("d-omega", {"json_path_equals": {"path": "answers.lane.value", "value": "omega"}}, agent_id="jev-decisions"),
        agent_task("d-alpha", {"json_path_equals": {"path": "answers.lane.value", "value": "alpha"}}, agent_id="jev-decisions"),
        agent_task("d-score", {"json_path_equals": {"path": "answers.risk.value", "value": 1.25}}, agent_id="jev-decisions"),
        agent_task("d-wrong-type", {"json_path_equals": {"path": "answers.risk.value", "value": "1.25"}},
                   agent_id="jev-decisions"),
        agent_task("d-missing", {"json_path_equals": {"path": "answers.nope.value", "value": "x"}}, agent_id="jev-decisions"),
        agent_task("d-run", {"status": "succeeded"}, {"max_tokens": 200}, agent_id="jev-decisions"),
    ]
    suite = load_suite(write_suite(tmp_path / "decisions.yaml", tasks))
    with serving(decision_worker):
        result = run_suite(DecisionAsAgent(MaefClient(live_api.transport)), suite, timeout_seconds=5)
    by_id = {t.task_id: t for t in result.tasks}
    assert by_id["d-omega"].passed and by_id["d-score"].passed and by_id["d-run"].passed
    assert not by_id["d-alpha"].passed and not by_id["d-wrong-type"].passed and not by_id["d-missing"].passed
    assert by_id["d-missing"].checks[0].detail == "path not found"


def test_unfinished_run_times_out_and_is_cancelled(live_api, tmp_path):
    suite = load_suite(write_suite(tmp_path / "slow.yaml", [agent_task("slow", {"status": "succeeded"})]))
    # No worker is serving, so the run stays queued past the timeout.
    result = run_suite(MaefClient(live_api.transport), suite, timeout_seconds=0.3)
    task = result.tasks[0]
    assert task.terminal_reason == "eval_timeout"
    assert task.status == "error"
    assert not task.passed
    run = live_api.ledger.get(task.run_id)
    assert (run.status, run.terminal_reason) == ("cancelled", "cancelled_by_user")


# --- comparison and report (pure) ---

def _result(revision, rows):
    tasks = [TaskResult(task_id=tid, status=status, tokens_used=tokens,
                        checks=[CheckResult(name="status:succeeded", passed=passed)])
             for tid, status, tokens, passed in rows]
    return SuiteResult(suite_id="s", revision=revision, tasks=tasks)


def test_compare_gives_pass_rates_token_totals_and_deltas():
    a = _result("A", [("t1", "succeeded", 100, True), ("t2", "failed", 50, False)])
    b = _result("B", [("t1", "succeeded", 150, True), ("t2", "succeeded", 40, True)])
    comparison = compare([a, b])

    rev_a, rev_b = comparison.revisions
    assert (rev_a.passed, rev_a.total, rev_a.pass_rate, rev_a.total_tokens, rev_a.mean_tokens) == (1, 2, 0.5, 150, 75.0)
    assert (rev_b.passed, rev_b.total, rev_b.pass_rate, rev_b.total_tokens, rev_b.mean_tokens) == (2, 2, 1.0, 190, 95.0)

    deltas = {t.task_id: [e.tokens_delta for e in t.entries] for t in comparison.tasks}
    assert deltas == {"t1": [None, 50], "t2": [None, -10]}


def test_markdown_report_has_header_and_both_revisions():
    comparison = compare([_result("A", [("t1", "succeeded", 100, True)]),
                          _result("B", [("t1", "failed", 90, False)])])
    text = comparison.to_markdown()
    assert "| task | A | B |" in text
    assert "| revision | passed | pass rate |" in text
    assert "| t1 | pass 100 | FAIL 90 (-10) [failed] |" in text


def test_compare_rejects_mixed_suites_and_nothing():
    with pytest.raises(ValueError):
        compare([])
    other = SuiteResult(suite_id="other", revision=None, tasks=[])
    with pytest.raises(ValueError):
        compare([_result("A", []), other])


# --- maef eval run ---

@pytest.fixture
def pinned_revisions(live_api, monkeypatch):
    """Revision names pick the definition: a 'bad' revision gets instructions the fake model answers wrongly."""
    base = definition(dirty=True)
    base = base.model_copy(update={"definition_digest": compute_definition_digest(base.definition_bytes, base.files)})
    bad = set()

    def resolve(agent_id, revision=None):
        text = f"bad rules {revision}" if revision in bad else f"rules {revision}"
        return base.model_copy(update={"instructions": text, "git_rev": revision or "working"})

    monkeypatch.setattr(main.runs.definitions, "resolve", resolve)

    class Rules:
        def converse(self, **kwargs):
            return ModelResponse("41" if "bad" in kwargs["system"] else "42", (), "end_turn", 100, 10)

    live_api.adapter.converse = Rules().converse
    return bad


def revision_suite(tmp_path):
    return write_suite(tmp_path / "rev.yaml", [agent_task("a", {"contains": "42"}, {"status": "succeeded"}),
                                               agent_task("b", {"contains": "42"})], suite_id="rev")


@pytest.mark.parametrize("bad, exit_code, rates", [
    ({"B"}, 1, [1.0, 0.0]),  # last revision fails: exit 1
    ({"A"}, 0, [0.0, 1.0]),  # only an earlier revision fails: exit 0
    (set(), 0, [1.0, 1.0]),
])
def test_cli_exit_code_follows_the_last_revision(live_api, pinned_revisions, tmp_path, capsys, bad, exit_code, rates):
    pinned_revisions.update(bad)
    with serving(agent_worker(live_api)):
        code = cli_main(["eval", "run", str(revision_suite(tmp_path)), "--revision", "A", "--revision", "B", "--json"],
                        client=MaefClient(live_api.transport))
    assert code == exit_code
    out = json.loads(capsys.readouterr().out)
    assert [r["revision"] for r in out["comparison"]["revisions"]] == ["A", "B"]
    assert [r["pass_rate"] for r in out["results"]] == rates


def test_cli_out_writes_markdown_report(live_api, pinned_revisions, tmp_path, capsys):
    report = tmp_path / "report.md"
    with serving(agent_worker(live_api)):
        code = cli_main(["eval", "run", str(revision_suite(tmp_path)), "--revision", "A", "--revision", "B",
                         "--out", str(report)], client=MaefClient(live_api.transport))
    assert code == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Eval `rev`")
    assert "| task | A | B |" in text
    assert "| a | pass 110 | pass 110 (+0) |" in text
