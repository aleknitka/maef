"""Benchmark report building and rendering, plus runner.report_existing / run_bench with a fake MAEF client."""

import json

import pytest
from maef_api.bench import report, runner
from maef_api.bench.families import FAMILIES
from maef_api.bench.families.base import STRAPLINE, Variant
from maef_api.bench.families.bowling import Bowling

FAM = Bowling()
VARIANT = Variant(
    family="bowling",
    seed=7,
    noise=2,
    params={"pins": 8, "frames": 6, "strike_bonus_rolls": 2, "spare_bonus_rolls": 1},
    names={"module": "lanes", "function": "tally"},
)
CANON = FAM.sample(0, 0)
RUN = {"run_id": "run-1", "status": "succeeded", "terminal_reason": None, "agent_revision": "rev-9"}
ALL_PASS = {"passed": 2, "failed": 0, "total": 2, "errors": []}


def usage(turn, inp, out, cost=None, cache=None, ts=None):
    p = {"turn": turn, "input_tokens": inp, "output_tokens": out}
    if cost is not None:
        p["cost_usd"] = cost
    if cache is not None:
        p["cache_read_tokens"] = cache
    e = {"type": "usage.recorded", "payload": p}
    if ts is not None:
        e["ts"] = ts
    return e


def build(events=(), rows=(), trace=None, grade_result=ALL_PASS, names=("lanes.py", "test_lanes.py", "NOTES.md")):
    return report.build(
        run=RUN,
        events=list(events),
        rows=list(rows),
        trace=trace,
        grade=grade_result,
        variant=VARIANT,
        family=FAM,
        canonical=CANON,
        agent_id="tdd-engineer",
        artifact_names=list(names),
    )


def test_token_and_cost_sums_across_turns():
    events = [usage(1, 100, 20, cost=0.01, cache=50), usage(2, 200, 50, cost=0.025, cache=0)]
    r = build(events=events)
    assert r["tokens"] == {"input": 300, "cached": 50, "output": 70, "reasoning": None, "total": 370}
    assert r["cost_usd"] == pytest.approx(0.035)
    assert r["turns"] == 2
    assert "- Cost: $0.0350" in report.render(r)


def test_missing_cost_is_reported_as_not_reported():
    r = build(events=[usage(1, 100, 20), usage(2, 10, 1)])
    assert r["cost_usd"] is None
    text = report.render(r)
    assert "- Cost: not reported" in text


def test_partial_cost_sums_only_the_turns_that_reported_it():
    # Observation, not a bug: the total covers reported turns only. Pinned so a change is deliberate.
    r = build(events=[usage(1, 100, 20, cost=0.01), usage(2, 10, 1)])
    assert r["cost_usd"] == pytest.approx(0.01)


def test_missing_transcript_is_handled():
    r = build(events=[usage(1, 5, 1, cost=0.001)], rows=[], trace=None)
    assert r["tdd"] is None
    assert r["turns"] == 1  # falls back to the number of per-turn rows
    assert r["tool_calls"] is None
    text = report.render(r)
    assert "No transcript was available, so no TDD analysis." in text
    assert "The agent never ran its tests." in text


def test_no_events_and_no_transcript_still_renders():
    r = build()
    assert r["wall_seconds"] is None and r["cost_usd"] is None and r["models"] == []
    text = report.render(r)
    assert "- Model: not reported; provider: not reported" in text
    assert "## Per-turn" in text


def test_events_fill_gaps_in_the_transcript_timeline_but_do_not_override_it():
    trace_timeline = [
        {
            "turn": 1,
            "t": 1.5,
            "latency_ms": None,
            "input_tokens": 999,
            "output_tokens": 1,
            "cost_usd": None,
            "tool_calls": 1,
        }
    ]
    events = [
        usage(1, 100, 20, cost=0.01),
        {"type": "model.response", "payload": {"turn": 1, "latency_ms": 900, "provider": "openrouter", "model": "m-x"}},
    ]
    trace = {"turns": 1, "tool_calls": 1, "timeline": trace_timeline, "tool_calls_by_kind": {}}
    r = build(events=events, trace=trace)
    row = r["per_turn"][0]
    assert row["latency_ms"] == 900  # filled from the event
    assert row["input_tokens"] == 999  # the transcript wins where it has a value
    assert row["cost_usd"] == 0.01  # filled from the event
    assert r["models"] == ["m-x"] and r["providers"] == ["openrouter"]


def test_wall_seconds_from_event_timestamps():
    events = [usage(1, 1, 1, ts="2026-11-24T10:00:00"), usage(2, 1, 1, ts="2026-11-24T10:00:30")]
    assert build(events=events)["wall_seconds"] == 30.0
    assert build(events=[usage(1, 1, 1, ts="not a time")])["wall_seconds"] is None


def test_grading_problem_and_pass_counts_render():
    assert "Grading problem: no podman or docker available" in report.render(
        build(
            grade_result={"passed": 0, "failed": 0, "total": 0, "errors": [], "error": "no podman or docker available"}
        )
    )
    failing = {"passed": 1, "failed": 1, "total": 2, "errors": [{"case": "gutter game", "expected": 0, "got": "2"}]}
    text = report.render(build(grade_result=failing))
    assert "**1 / 2 passed** (1 failed)" in text
    assert "- `gutter game`: expected 0, got 2" in text


def test_deliverables_present_and_missing():
    r = build(names=("/workspace/out/lanes.py", "NOTES.md"))
    assert r["deliverables"]["expected"] == ["lanes.py", "test_lanes.py", "NOTES.md"]
    assert r["deliverables"]["present"] == ["lanes.py", "NOTES.md"]
    assert r["deliverables"]["missing"] == ["test_lanes.py"]


def test_render_header_and_variant_table():
    text = report.render(build())
    assert text.startswith("# Benchmark report: bowling seed 7 noise 2")
    assert STRAPLINE in text
    assert "| pins | 8 | 10 |" in text
    assert "| name: function | tally | score |" in text
    assert "rev-9" in text


def test_tdd_section_renders_verdict_and_test_runs():
    trace_result = {
        "turns": 3,
        "tool_calls": 3,
        "tool_calls_by_kind": {"file_write": 2, "test_run": 1},
        "timeline": [],
        "verdict": "test-first (never red)",
        "first_test_file_write": 1,
        "first_impl_write": 2,
        "tests_written_first": True,
        "first_test_run_turn": 3,
        "first_test_run_failed": False,
        "red_to_green_cycles": 0,
        "turns_to_first_green": 3,
        "final_tests_green": True,
        "rewrites": {"/workspace/lanes.py": 1},
        "test_runs": [{"turn": 3, "passed": True, "ran": 4, "failures": 0, "errors": 0}],
        "final_test_result": {"turn": 3, "passed": True, "ran": 4, "failures": 0, "errors": 0},
    }
    text = report.render(build(trace=trace_result))
    assert "**Verdict: test-first (never red)**" in text
    assert "| 3 | True | 4 | 0 | 0 |" in text
    assert "/workspace/lanes.py x1" in text
    assert "Last run at turn 3: passed (ran 4, failures 0, errors 0)" in text


def test_git_revision_falls_back_to_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(report.subprocess, "run", boom)
    assert report.git_revision() == "unknown"


def test_git_revision_is_a_short_hash_in_this_repo():
    rev = report.git_revision()
    assert rev == "unknown" or (4 <= len(rev) <= 40 and all(c in "0123456789abcdef" for c in rev))


def test_ts_parses_numbers_and_iso_strings_only():
    assert report._ts(12) == 12.0
    assert report._ts(None) is None
    assert report._ts("nope") is None


def test_ts_rejects_bool():
    assert report._ts(True) is None


# --- runner with a fake client ---------------------------------------------------------------------


class FakeClient:
    def __init__(self, run_id="run-1", artifacts=None, events=(), run=None):
        self.run_id, self._artifacts, self._events, self._run = run_id, artifacts or [], list(events), run or RUN
        self.blobs = {}
        self.submitted = []

    def artifacts(self, run_id):
        return list(self._artifacts)

    def download(self, digest):
        return self.blobs[digest]

    def events(self, run_id, deadline=None):
        return iter(self._events)

    def get_run(self, run_id):
        return dict(self._run)

    def submit(self, agent_id, task):
        self.submitted.append((agent_id, task))
        return {"run_id": self.run_id}

    def wait(self, run_id, timeout, on_event=None):
        return {"status": "succeeded", "terminal_reason": None}


def transcript_bytes():
    rows = [
        {
            "type": "assistant",
            "turn": 1,
            "tool_calls": [
                {"id": "a", "name": "run_command", "arguments": {"argv": ["tee", "test_lanes.py"], "stdin": "x"}}
            ],
        },
        {
            "type": "tool",
            "turn": 1,
            "id": "a",
            "name": "run_command",
            "arguments": {"argv": ["tee", "test_lanes.py"], "stdin": "x"},
            "content": json.dumps({"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}),
            "is_error": False,
        },
    ]
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


def test_report_existing_writes_all_run_files(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.grade, "run_grader", lambda src, cases, d: dict(ALL_PASS))
    blobs = {"d1": b"def tally(r): ...", "d2": transcript_bytes()}
    client = FakeClient(
        artifacts=[{"name": "lanes.py", "digest": "d1"}, {"name": "transcript.jsonl", "digest": "d2"}],
        events=[usage(1, 50, 5, cost=0.002, ts="2026-11-24T10:00:00")],
    )
    client.blobs = blobs
    rep = runner.report_existing(client, "run-1", "bowling", 7, 2, tmp_path)
    run_dir = tmp_path / "run-1"
    for name in (
        "task.md",
        "variant.json",
        "cases.json",
        "events.jsonl",
        "report.json",
        "report.md",
        "artifacts/lanes.py",
        "artifacts/transcript.jsonl",
    ):
        assert (run_dir / name).exists(), name
    assert rep["tdd"]["verdict"] == "test-first (tests never run)"
    assert rep["cost_usd"] == pytest.approx(0.002)
    assert "**2 / 2 passed** (0 failed)" in (run_dir / "report.md").read_text(encoding="utf-8")
    assert json.loads((run_dir / "report.json").read_text(encoding="utf-8"))["family"] == "bowling"


def test_run_bench_submits_the_rendered_task_and_grades(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.grade, "run_grader", lambda src, cases, d: dict(ALL_PASS))
    client = FakeClient(run_id="run-42")
    rep = runner.run_bench(client, "bowling", 7, 2, "tdd-engineer", tmp_path, 10)
    assert client.submitted == [("tdd-engineer", FAM.render_task(FAM.sample(7, 2)))]
    assert rep["run_id"] == "run-42"
    assert (tmp_path / "run-42" / "report.md").exists()


def test_download_artifacts_keeps_files_inside_the_destination(tmp_path):
    client = FakeClient(artifacts=[{"name": "../../escape.txt", "digest": "d"}])
    client.blobs = {"d": b"x"}
    names = runner.download_artifacts(client, "run-1", tmp_path / "artifacts")
    assert names == ["../../escape.txt"]
    assert (tmp_path / "artifacts" / "escape.txt").read_bytes() == b"x"
    assert not (tmp_path / "escape.txt").exists()


def test_runner_uses_the_family_registry():
    assert runner.FAMILIES is FAMILIES
    assert runner._safe_name("a/./b/../c").as_posix() == "a/b/c"
