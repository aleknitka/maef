"""Transcript analysis: TDD verdicts, red-to-green cycles, test-result parsing and write classification."""

import json

import pytest

from maef_api.bench import trace

PASS_OK = {"exit_code": 0, "stdout": "", "stderr": "Ran 3 tests\n\nOK", "timed_out": False}
RED = {"exit_code": 1, "stdout": "", "stderr": "Ran 3 tests\n\nFAILED (failures=1, errors=2)", "timed_out": False}
UNITTEST = ["python3", "-m", "unittest", "-v"]


class Run:
    """Builds transcript rows: one assistant turn with one run_command call, then its tool result."""

    def __init__(self):
        self.rows, self.turn, self.n = [], 0, 0

    def step(self, argv, stdin=None, result=None, name="run_command"):
        self.turn += 1
        self.n += 1
        call_id = f"c{self.n}"
        args = {"argv": argv, "stdin": stdin}
        self.rows.append(
            {
                "type": "assistant",
                "turn": self.turn,
                "t": float(self.turn),
                "latency_ms": 100,
                "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
                "tool_calls": [{"id": call_id, "name": name, "arguments": args}],
            }
        )
        if result is not None:
            self.rows.append(
                {
                    "type": "tool",
                    "turn": self.turn,
                    "id": call_id,
                    "name": name,
                    "arguments": args,
                    "content": json.dumps(result),
                    "is_error": False,
                }
            )
        return self

    def write(self, path, role_content=""):
        return self.step(
            ["tee", path], stdin=role_content, result={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        )

    def run(self, result):
        return self.step(UNITTEST, result=result)

    def text(self):
        return "\n".join(json.dumps(r) for r in self.rows) + "\n"


def analyse(run: Run):
    return trace.analyse(trace.load(run.text()))


# --- TDD verdicts ----------------------------------------------------------------------------------


def test_test_first_red_to_green():
    r = (
        Run()
        .write("test_bowling.py", "def test_x(): pass")
        .run(RED)
        .write("bowling.py", "def score(r): ...")
        .run(PASS_OK)
    )
    a = analyse(r)
    assert a["verdict"] == "test-first (red→green)"
    assert a["tests_written_first"] is True
    assert a["first_test_file_write"] == 1
    assert a["first_impl_write"] == 3
    assert a["first_test_run_turn"] == 2
    assert a["first_test_run_failed"] is True
    assert a["red_to_green_cycles"] == 1
    assert a["turns_to_first_green"] == 4
    assert a["final_tests_green"] is True
    assert a["turns"] == 4 and a["tool_calls"] == 4
    assert a["tool_calls_by_kind"] == {"file_write": 2, "test_run": 2}


def test_tests_after_code():
    r = Run().write("bowling.py", "def score(r): ...").write("test_bowling.py", "def test_x(): pass").run(PASS_OK)
    a = analyse(r)
    assert a["verdict"] == "tests after code"
    assert a["tests_written_first"] is False
    assert a["first_impl_write"] == 1 and a["first_test_file_write"] == 2
    assert a["turns_to_first_green"] == 3
    assert a["red_to_green_cycles"] == 0


def test_no_tests_written_or_run():
    a = analyse(Run().write("bowling.py", "x = 1").write("NOTES.md", "notes"))
    assert a["verdict"] == "no tests"
    assert a["first_test_file_write"] is None
    assert a["test_runs"] == []
    assert a["first_test_run_turn"] is None
    assert a["final_tests_green"] is None
    assert a["final_test_result"] is None
    assert a["turns_to_first_green"] is None


def test_red_never_green():
    red2 = {"exit_code": 1, "stdout": "", "stderr": "Ran 2 tests\n\nFAILED (failures=2)", "timed_out": False}
    r = Run().write("test_bowling.py", "x").run(RED).write("bowling.py", "y").run(red2)
    a = analyse(r)
    assert a["verdict"] == "test-first (red, never green)"
    assert a["red_to_green_cycles"] == 0
    assert a["turns_to_first_green"] is None
    assert a["final_tests_green"] is False


def test_green_on_first_run_is_never_red():
    a = analyse(Run().write("test_bowling.py", "x").run(PASS_OK).write("bowling.py", "y"))
    assert a["verdict"] == "test-first (never red)"
    assert a["first_test_run_failed"] is False
    assert a["red_to_green_cycles"] == 0


def test_tests_written_but_never_run():
    a = analyse(Run().write("test_bowling.py", "x").write("bowling.py", "y"))
    assert a["verdict"] == "test-first (tests never run)"
    assert a["test_runs"] == []
    assert a["tests_written_first"] is True


def test_two_red_to_green_cycles_are_counted():
    r = Run().write("test_bowling.py", "x").run(RED).run(PASS_OK).run(RED).run(PASS_OK)
    a = analyse(r)
    assert a["red_to_green_cycles"] == 2
    assert a["turns_to_first_green"] == 3
    assert [t["passed"] for t in a["test_runs"]] == [False, True, False, True]


def test_rewrites_count_repeated_writes_to_the_same_file():
    a = analyse(Run().write("bowling.py", "a").write("bowling.py", "b").write("bowling.py", "c"))
    assert a["rewrites"] == {"/workspace/bowling.py": 2}


def test_timeline_carries_usage_and_tool_counts():
    a = analyse(Run().write("bowling.py", "a"))
    assert a["timeline"] == [
        {
            "turn": 1,
            "t": 1.0,
            "latency_ms": 100,
            "input_tokens": 10,
            "cache_read_tokens": None,
            "output_tokens": 5,
            "reasoning_tokens": None,
            "cost_usd": 0.01,
            "tool_calls": 1,
        }
    ]


def test_non_command_tools_are_counted_separately():
    r = Run().step(["x"], result={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}, name="read_file")
    a = trace.analyse(trace.load(r.text()))
    assert a["tool_calls_by_kind"] == {"other_tool": 1}
    assert a["tool_calls_by_name"] == {"read_file": 1}
    assert a["verdict"] == "no tests"


def test_empty_and_garbage_lines_are_skipped():
    text = "\nnot json\n" + json.dumps([1, 2]) + "\n" + Run().write("bowling.py", "a").text()
    rows = trace.load(text)
    assert len(rows) == 2  # the list and the non-JSON line are dropped; the blank line too
    assert trace.analyse(rows)["rewrites"] == {"/workspace/bowling.py": 0}  # one write, zero rewrites


# --- test-result parsing ---------------------------------------------------------------------------


def test_parse_unittest_failure_counts():
    res = trace.parse_test_result(json.dumps(RED))
    assert res == {"passed": False, "ran": 3, "failures": 1, "errors": 2}


def test_parse_unittest_ok():
    assert trace.parse_test_result(json.dumps(PASS_OK)) == {"passed": True, "ran": 3, "failures": 0, "errors": 0}


def test_parse_singular_test():
    body = {"exit_code": 0, "stdout": "", "stderr": "Ran 1 test\n\nOK", "timed_out": False}
    assert trace.parse_test_result(json.dumps(body))["ran"] == 1


def test_parse_pytest_summary():
    body = {"exit_code": 1, "stdout": "=== 2 passed, 1 failed, 1 error in 0.10s ===", "stderr": "", "timed_out": False}
    assert trace.parse_test_result(json.dumps(body)) == {"passed": False, "ran": 3, "failures": 1, "errors": 1}


def test_exit_zero_with_failed_summary_is_not_a_pass():
    body = {"exit_code": 0, "stdout": "", "stderr": "Ran 2 tests\n\nFAILED (errors=1)", "timed_out": False}
    assert trace.parse_test_result(json.dumps(body))["passed"] is False


def test_timeout_is_not_a_pass():
    body = dict(PASS_OK, timed_out=True)
    assert trace.parse_test_result(json.dumps(body))["passed"] is False


def test_unparseable_content_falls_back_to_text_and_no_exit_code():
    res = trace.parse_test_result("Ran 4 tests\n\nOK")
    assert res == {"passed": True, "ran": 4, "failures": 0, "errors": 0}
    assert trace.parse_test_result("Ran 4 tests\n\nFAILED (failures=3)")["passed"] is False


# --- write and run classification -----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, stdin, path, role",
    [
        (["python3", "-c", "open('bowling.py', 'w').write('x')"], None, "/workspace/bowling.py", "impl"),
        (
            ["python3", "-c", "from pathlib import Path; Path('tests/test_b.py').write_text('x')"],
            None,
            "/workspace/tests/test_b.py",
            "test",
        ),
        (["sh", "-c", "sed -i 's/a/b/' bowling.py"], None, "/workspace/bowling.py", "impl"),
        (["tee", "-a", "/workspace/out/test_bowling.py"], "x", "/workspace/out/test_bowling.py", "test"),
        (["sh", "-c", "echo hi > NOTES.md"], None, "/workspace/NOTES.md", "doc"),
        (["sh", "-c", "echo hi > /dev/null"], None, None, None),
    ],
)
def test_classify_write_targets(argv, stdin, path, role):
    info = trace.classify(argv, stdin)
    if path is None:
        assert info["writes"] == []
    else:
        assert info["writes"] == [(path, role)]


def test_heredoc_body_mentioning_pytest_is_not_a_test_run():
    info = trace.classify(["sh", "-c", "cat > test_x.py <<'EOF'\nimport pytest\nEOF"], None)
    assert info["test_run"] is False
    assert info["writes"] == [("/workspace/test_x.py", "test")]


@pytest.mark.parametrize(
    "argv", [UNITTEST, ["pytest", "-q"], ["python3", "test_bowling.py"], ["python3", "-m", "pytest"]]
)
def test_recognised_test_commands(argv):
    assert trace.classify(argv, None)["test_run"] is True


def test_plain_command_is_not_a_test_run():
    assert trace.classify(["ls", "-la"], None) == {"writes": [], "test_run": False}


def test_a_single_call_can_write_and_run_tests():
    info = trace.classify(["sh", "-c", "cat > test_bowling.py <<'EOF'\nx\nEOF\npython3 -m unittest -v"], None)
    assert info["writes"] == [("/workspace/test_bowling.py", "test")]
    assert info["test_run"] is True


def test_test_run_inside_shell_stdin_is_detected():
    info = trace.classify(["bash"], "cd /workspace && python3 -m unittest -v\n")
    assert info["test_run"] is True


def test_file_content_on_stdin_is_not_a_test_run():
    info = trace.classify(["sh", "-c", "cat > /workspace/out/test_x.py"], "import unittest\nunittest.main()\n")
    assert info["test_run"] is False
