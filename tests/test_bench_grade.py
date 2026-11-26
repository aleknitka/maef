"""Hidden-case grading. Non-container tests cover error handling and result parsing. The `oci` tests run the
reference and a broken solution through the real hardened podman/docker grader."""

import inspect
import json
import textwrap

import pytest

from maef_api.bench import grade
from maef_api.bench.families.bowling import Bowling, reference_score
from maef_api.sandbox import detect_sandbox

FAM = Bowling()
SANDBOX = detect_sandbox()


def reference_module(v) -> str:
    """A deliverable that is the reference rule set exposed under the variant's module and function names."""
    p = v.params
    body = textwrap.dedent(inspect.getsource(reference_score))
    return (
        body + "\n\n"
        f"def {v.names['function']}(rolls):\n"
        f"    return reference_score(rolls, {p['pins']}, {p['frames']}, {p['strike_bonus_rolls']}, "
        f"{p['spare_bonus_rolls']})\n"
    )


class _NoRuntime:
    runtime = "podman"


# --- no container runtime (no real containers needed) -----------------------------------------------


def test_no_runtime_is_an_error_result_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(grade, "detect_sandbox", lambda: None)
    res = grade.run_grader("print('x')", [{"name": "a", "expected": 1}], tmp_path)
    assert res == {"passed": 0, "failed": 0, "total": 0, "errors": [], "error": "no podman or docker available"}


def test_runtime_binary_missing_is_an_error_result(tmp_path, monkeypatch):
    monkeypatch.setattr(grade, "detect_sandbox", lambda: _NoRuntime())
    monkeypatch.setattr(grade.shutil, "which", lambda name: None)
    res = grade.run_grader("print('x')", [], tmp_path)
    assert res["error"] == "no podman or docker available" and res["total"] == 0


def test_unexpected_exception_is_reported_not_raised(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(grade, "detect_sandbox", boom)
    res = grade.run_grader("print('x')", [], tmp_path)
    assert res["error"] == "grading failed: RuntimeError: boom"


def test_parse_output_takes_the_last_result_line():
    out = (
        "noise\n"
        + json.dumps({"passed": 0, "failed": 9, "errors": []})
        + "\n"
        + json.dumps({"passed": 3, "failed": 1, "errors": [{"case": "c", "expected": 1, "got": "2"}]})
        + "\n"
    )
    res = grade.parse_output(out, "", 0, 4)
    assert res == {"passed": 3, "failed": 1, "total": 4, "errors": [{"case": "c", "expected": 1, "got": "2"}]}


def test_parse_output_without_result_line_is_an_error_with_stderr_tail():
    res = grade.parse_output("Traceback (most recent call last)...\n", "E" * 500 + "KABOOM", 1, 5)
    assert res["error"].startswith("no result line from the grader (exit 1): ")
    assert res["error"].endswith("KABOOM") and len(res["error"]) < 400
    assert res["passed"] == 0 and res["failed"] == 0 and res["total"] == 0  # the error shape, as _error builds it


def test_parse_output_caps_errors_at_twenty():
    many = [{"case": f"c{i}", "expected": 0, "got": "1"} for i in range(50)]
    res = grade.parse_output(json.dumps({"passed": 0, "failed": 50, "errors": many}), "", 0, 50)
    assert len(res["errors"]) == 20 and res["total"] == 50


def test_stage_deliverables_flattens_and_skips_run_metadata(tmp_path):
    src, dest = tmp_path / "artifacts", tmp_path / "work"
    (src / "sub").mkdir(parents=True)
    dest.mkdir()
    (src / "sub" / "lanes.py").write_text("def f(): pass")
    (src / "test_lanes.py").write_text("x")
    (src / "NOTES.md").write_text("notes")
    (src / "output.md").write_text("skip me")
    (src / "transcript.jsonl").write_text("skip me too")
    names = grade.stage_deliverables(src, dest)
    assert names == ["NOTES.md", "lanes.py", "test_lanes.py"]
    assert (dest / "lanes.py").read_text() == "def f(): pass"
    assert not (dest / "output.md").exists() and not (dest / "transcript.jsonl").exists()


def test_error_shape_and_image_pin():
    assert grade._error("x") == {"passed": 0, "failed": 0, "total": 0, "errors": [], "error": "x"}
    assert grade._image().startswith("docker.io/library/python@sha256:")


# --- real container grader ----------------------------------------------------------------------------

requires_runtime = pytest.mark.skipif(SANDBOX is None, reason="no podman or docker runtime found")


@pytest.mark.oci
@requires_runtime
@pytest.mark.parametrize("seed, noise", [(0, 0), (7, 2)])
def test_reference_solution_passes_every_hidden_case(tmp_path, seed, noise):
    v = FAM.sample(seed, noise)
    cases = FAM.hidden_cases(v)
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / f"{v.names['module']}.py").write_text(reference_module(v), encoding="utf-8")
    res = grade.run_grader(FAM.grader_source(v), cases, art)
    assert "error" not in res, res
    assert res["total"] == len(cases)
    assert res["passed"] == len(cases) and res["failed"] == 0, res["errors"]


@pytest.mark.oci
@requires_runtime
def test_broken_solution_fails_the_cases_it_gets_wrong(tmp_path):
    v = FAM.sample(0, 0)
    cases = FAM.hidden_cases(v)
    art = tmp_path / "artifacts"
    art.mkdir()
    # Ignores bonuses entirely: wrong for every spare and strike, right for gutter and invalid-input cases.
    (art / "bowling.py").write_text(
        "def score(rolls):\n    if not rolls:\n        raise ValueError('empty')\n    return sum(rolls)\n",
        encoding="utf-8",
    )
    res = grade.run_grader(FAM.grader_source(v), cases, art)
    assert "error" not in res, res
    assert res["total"] == len(cases)
    assert res["failed"] > 0 and res["passed"] > 0
    assert res["passed"] + res["failed"] == len(cases)
    names = {e["case"] for e in res["errors"]}
    assert "single spare" in names or "all spares" in names
    assert "gutter game" not in names  # the broken solution does get the gutter game right
    assert len(res["errors"]) <= 20


@pytest.mark.oci
@requires_runtime
def test_missing_deliverable_fails_every_case_with_an_import_error(tmp_path):
    v = FAM.sample(0, 0)
    cases = FAM.hidden_cases(v)
    art = tmp_path / "artifacts"
    art.mkdir()
    res = grade.run_grader(FAM.grader_source(v), cases, art)
    assert "error" not in res, res
    assert res["passed"] == 0 and res["failed"] == len(cases)
    assert res["errors"] and "import failed" in res["errors"][0]["got"]
