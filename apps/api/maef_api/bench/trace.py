"""Analyse a transcript.jsonl into TDD metrics. Heuristic: only `run_command` calls are classified, by regex over
the joined argv. See docs/BENCHMARKS.md for the definitions."""

import json
import re
from collections import Counter
from itertools import pairwise
from typing import Any

_REDIRECT = re.compile(r"""(?<![0-9&<>-])>>?\s*['"]?([^\s'"<>|;&()]+)""")
_TEE = re.compile(r"""\btee\s+(?:-a\s+)?['"]?([^\s'"<>|;&()]+)""")
_PYOPEN = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wax]""")
_PYPATH = re.compile(r"""Path\(\s*['"]([^'"]+)['"]\s*\)\s*\.write_(?:text|bytes)""")
_SEDI = re.compile(r"""\bsed\s+-i\S*\s+(?:-e\s+)?(?:'[^']*'|"[^"]*"|\S+)\s+(\S+)""")
_HEREDOC = re.compile(r"""<<-?\s*['"]?(\w+)['"]?[^\n]*\n.*?\n\s*\1\b""", re.DOTALL)
_RUN = re.compile(r"""(?:-m\s+(?:unittest|pytest)\b|\bpytest\b|\bunittest\b|\bpython3?\s+(?:\S*/)?test\w*\.py\b)""")
_RAN = re.compile(r"Ran (\d+) tests?")
_FAILED = re.compile(r"FAILED \(([^)]*)\)")
_PYTEST = re.compile(r"(\d+) (passed|failed|error)")


def _norm(path: str) -> str:
    path = path.strip()
    return path if path.startswith("/") else "/workspace/" + path.removeprefix("./")


def _role(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base.startswith("test_") or base.endswith("_test.py") or "/tests/" in path:
        return "test"
    if base.endswith(".py"):
        return "impl"
    return "doc"


def classify(argv: list[str], stdin: str | None) -> dict:
    """-> {"writes": [(path, role)], "test_run": bool}. A single call can both write and run tests."""
    text = " ".join(argv)
    body = text + ("\n" + stdin if stdin and _looks_like_shell(argv) else "")
    paths: list[str] = []
    for rx in (_REDIRECT, _TEE, _PYOPEN, _PYPATH, _SEDI):
        paths += rx.findall(body)
    if stdin and argv and argv[0] in ("tee",) and len(argv) > 1:
        paths.append(argv[-1])
    writes = []
    for p in dict.fromkeys(paths):
        if p.startswith("/dev/") or not re.fullmatch(r"[\w./~-]*\.\w+", p):
            continue
        writes.append((_norm(p), _role(p)))
    # A shell reading its script from stdin can run tests there; stdin that is file content being written
    # (cat > x, tee x) must not count, since test files themselves mention unittest.
    run_text = text
    if stdin and _looks_like_shell(argv) and len(argv) == 1:
        run_text += "\n" + stdin
    return {"writes": writes, "test_run": bool(_RUN.search(_HEREDOC.sub(" ", run_text)))}


def _looks_like_shell(argv: list[str]) -> bool:
    return bool(argv) and argv[0].rsplit("/", 1)[-1] in ("sh", "bash", "dash", "ash")


def parse_test_result(content: str) -> dict:
    """-> {"passed", "ran", "failures", "errors"} from a tool result (JSON {exit_code,stdout,stderr,timed_out})."""
    try:
        body = json.loads(content)
        text = f"{body.get('stdout', '')}\n{body.get('stderr', '')}"
        exit_code = body.get("exit_code")
        timed_out = bool(body.get("timed_out"))
    except (ValueError, AttributeError):
        text, exit_code, timed_out = str(content), None, False
    ran = int(m.group(1)) if (m := _RAN.search(text)) else 0
    failures = errors = 0
    if m := _FAILED.search(text):
        failures = int(g.group(1)) if (g := re.search(r"failures=(\d+)", m.group(1))) else 0
        errors = int(g.group(1)) if (g := re.search(r"errors=(\d+)", m.group(1))) else 0
    for count, word in _PYTEST.findall(text):
        if word == "passed":
            ran += int(count)
        elif word == "failed":
            failures += int(count)
            ran += int(count)
        else:
            errors += int(count)
    ok = exit_code == 0 if exit_code is not None else bool(re.search(r"^OK\b", text, re.MULTILINE))
    passed = ok and not timed_out and not failures and not errors
    return {"passed": passed, "ran": ran, "failures": failures, "errors": errors}


def load(path_or_text: str) -> list[dict]:
    rows = []
    for line in path_or_text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def analyse(rows: list[dict]) -> dict:
    assistants = [r for r in rows if r.get("type") == "assistant"]
    tools = [r for r in rows if r.get("type") == "tool"]
    by_name = Counter(r.get("name") for r in tools)
    kinds: Counter = Counter()
    first_test: tuple[Any, int] | None = None  # (turn, order) positions
    first_impl: tuple[Any, int] | None = None
    test_runs: list[dict] = []
    writes: Counter = Counter()
    for order, r in enumerate(tools):
        turn = r.get("turn")
        if r.get("name") != "run_command":
            kinds["other_tool"] += 1
            continue
        raw_args = r.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {}
        raw_argv = args.get("argv")
        argv = raw_argv if isinstance(raw_argv, list) else []
        stdin = args.get("stdin")
        info = classify([str(a) for a in argv], stdin if isinstance(stdin, str) else None)
        if info["writes"]:
            kinds["file_write"] += 1
        if info["test_run"]:
            kinds["test_run"] += 1
        if not info["writes"] and not info["test_run"]:
            kinds["other_command"] += 1
        for path, role in info["writes"]:
            writes[path] += 1
            if role == "test" and first_test is None:
                first_test = (turn, order)
            if role == "impl" and first_impl is None:
                first_impl = (turn, order)
        if info["test_run"]:
            test_runs.append({"turn": turn, **parse_test_result(r.get("content", ""))})
    green = [t for t in test_runs if t["passed"]]
    cycles = sum(1 for a, b in pairwise(test_runs) if not a["passed"] and b["passed"])
    tests_first = first_test is not None and (first_impl is None or first_test[1] < first_impl[1])
    verdict = _verdict(first_test is not None, tests_first, test_runs, bool(green))
    return {
        "turns": len(assistants),
        "tool_calls": sum(by_name.values()),
        "tool_calls_by_name": dict(by_name),
        "tool_calls_by_kind": dict(kinds),
        "first_test_file_write": first_test[0] if first_test else None,
        "first_impl_write": first_impl[0] if first_impl else None,
        "tests_written_first": tests_first,
        "first_test_run_turn": test_runs[0]["turn"] if test_runs else None,
        "first_test_run_failed": (not test_runs[0]["passed"]) if test_runs else None,
        "test_runs": test_runs,
        "red_to_green_cycles": cycles,
        "turns_to_first_green": green[0]["turn"] if green else None,
        "final_tests_green": test_runs[-1]["passed"] if test_runs else None,
        "final_test_result": test_runs[-1] if test_runs else None,
        "rewrites": {p: n - 1 for p, n in writes.items()},
        "verdict": verdict,
        "timeline": [_timeline(a) for a in assistants],
    }


def _verdict(wrote_tests: bool, tests_first: bool, runs: list[dict], any_green: bool) -> str:
    if not wrote_tests and not runs:
        return "no tests"
    if not tests_first:
        return "tests after code"
    if not runs:
        return "test-first (tests never run)"
    if not runs[0]["passed"]:
        return "test-first (red→green)" if any_green else "test-first (red, never green)"
    return "test-first (never red)"


def _timeline(a: dict) -> dict:
    u = a.get("usage") or {}
    return {
        "turn": a.get("turn"),
        "t": a.get("t"),
        "latency_ms": a.get("latency_ms"),
        "input_tokens": u.get("input_tokens"),
        "cache_read_tokens": u.get("cache_read_tokens"),
        "output_tokens": u.get("output_tokens"),
        "reasoning_tokens": u.get("reasoning_tokens"),
        "cost_usd": u.get("cost_usd"),
        "tool_calls": len(a.get("tool_calls") or []),
    }
