"""Golden-task evals: run a suite of tasks against an agent revision and compare revisions on quality and tokens.

Interface: `load_suite(path)`, `run_suite(client, suite, revision=..., timeout_seconds=...)` and `compare(results)`.
Everything goes through MaefClient, so evals use the same auth, admission and budget limits as any other caller.
Checks run on the run's result text (output.md), its status and token use, and, for decision-style runs, on the
answers.json artifact. Result text is only used to evaluate checks; reports carry check names and short details,
never the text itself.
"""

import contextlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .client import TERMINAL_STATUSES, MaefClient, MaefClientError
from .models import BudgetOverride

_MISSING = object()


class JsonPathEquals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=200, description="dotted path, e.g. answers.ticket_route.value")
    value: str | int | float | bool | None


class Check(BaseModel):
    """Exactly one of the fields is set."""

    model_config = ConfigDict(extra="forbid")
    contains: str | None = None
    not_contains: str | None = None
    regex: str | None = None
    max_tokens: int | None = Field(default=None, ge=0)
    status: Literal["succeeded", "failed", "cancelled"] | None = None
    json_path_equals: JsonPathEquals | None = None

    @model_validator(mode="after")
    def _one(self):
        if len(self.model_fields_set) != 1:
            raise ValueError(
                "a check sets exactly one of contains, not_contains, regex, max_tokens, status, json_path_equals"
            )
        return self

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value):
        if value is not None:
            re.compile(value)
        return value

    @property
    def name(self) -> str:
        key = next(iter(self.model_fields_set))
        arg = getattr(self, key)
        return f"{key}:{arg.path if isinstance(arg, JsonPathEquals) else arg}"[:120]


class EvalTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    agent_id: str
    instruction: str = Field(min_length=1, max_length=20000)
    budget: BudgetOverride | None = None
    checks: list[Check] = Field(min_length=1)


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    tasks: list[EvalTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self):
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        return self


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class TaskResult(BaseModel):
    task_id: str
    run_id: str | None = None
    status: str
    terminal_reason: str | None = None
    tokens_used: int | None = None
    duration_seconds: float | None = None
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


class SuiteResult(BaseModel):
    suite_id: str
    revision: str | None = None
    tasks: list[TaskResult]

    @property
    def passed(self) -> int:
        return sum(t.passed for t in self.tasks)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.tasks) if self.tasks else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens_used or 0 for t in self.tasks)

    @property
    def failed_tasks(self) -> list[str]:
        return [t.task_id for t in self.tasks if not t.passed]


def load_suite(path: Path | str) -> EvalSuite:
    return EvalSuite.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _json_path(data, path: str):
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return _MISSING
    return data


def _check(check: Check, *, status: str, tokens: int | None, text: str, answers) -> CheckResult:
    name = check.name
    if check.contains is not None:
        return CheckResult(name=name, passed=check.contains in text)
    if check.not_contains is not None:
        return CheckResult(name=name, passed=check.not_contains not in text)
    if check.regex is not None:
        return CheckResult(name=name, passed=re.search(check.regex, text) is not None)
    if check.max_tokens is not None:
        return CheckResult(name=name, passed=tokens is not None and tokens <= check.max_tokens, detail=f"used {tokens}")
    if check.status is not None:
        return CheckResult(name=name, passed=status == check.status, detail=f"was {status}")
    spec = check.json_path_equals
    found = _json_path(answers, spec.path) if answers is not None else _MISSING
    if found is _MISSING:
        return CheckResult(name=name, passed=False, detail="path not found")
    return CheckResult(
        name=name,
        passed=found == spec.value and type(found) is type(spec.value),
        detail="" if found == spec.value else "value differs",
    )


def _answers(client: MaefClient, run_id: str, text: str):
    """answers.json when the run produced one, else the result text if it is JSON, else None."""
    try:
        found = client.answers(run_id)
    except MaefClientError:
        found = None
    if found is not None:
        return found
    try:
        return json.loads(text)
    except ValueError:
        return None


def _seconds(run: dict) -> float | None:
    try:
        return round(
            (datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["created_at"])).total_seconds(), 3
        )
    except (KeyError, TypeError, ValueError):
        return None


def _run_task(client: MaefClient, task: EvalTask, revision: str | None, timeout_seconds: float) -> TaskResult:
    budget = task.budget.model_dump(exclude_none=True) if task.budget else None
    try:
        run_id = client.submit(task.agent_id, task.instruction, budget, revision)["run_id"]
        run = client.wait(run_id, timeout_seconds)
    except MaefClientError as exc:
        return TaskResult(
            task_id=task.id,
            status="error",
            terminal_reason=f"api_error:{exc.code}",
            checks=[CheckResult(name="run", passed=False, detail=f"{exc.code}")],
        )
    reason = run.get("terminal_reason")
    if run["status"] not in TERMINAL_STATUSES:
        reason = "eval_timeout"
        with contextlib.suppress(MaefClientError):
            client.cancel(run_id)
    text, answers = "", None
    try:
        text = client.result_text(run_id) or ""
        if any(c.json_path_equals for c in task.checks):
            answers = _answers(client, run_id, text)
    except MaefClientError:
        pass
    tokens = run.get("tokens_used")
    results = [_check(c, status=run["status"], tokens=tokens, text=text, answers=answers) for c in task.checks]
    status = "error" if reason == "eval_timeout" else run["status"]
    return TaskResult(
        task_id=task.id,
        run_id=run_id,
        status=status,
        terminal_reason=reason,
        tokens_used=tokens,
        duration_seconds=_seconds(run),
        checks=results,
    )


def run_suite(
    client: MaefClient, suite: EvalSuite, *, revision: str | None = None, timeout_seconds: float = 600, on_task=None
) -> SuiteResult:
    """Run every task sequentially at `revision` (None: the working tree). `on_task(TaskResult)` reports progress."""
    results = []
    for task in suite.tasks:
        result = _run_task(client, task, revision, timeout_seconds)
        results.append(result)
        if on_task:
            on_task(result)
    return SuiteResult(suite_id=suite.id, revision=revision, tasks=results)


class RevisionSummary(BaseModel):
    revision: str | None
    passed: int
    total: int
    pass_rate: float
    total_tokens: int
    mean_tokens: float


class TaskEntry(BaseModel):
    revision: str | None
    passed: bool
    tokens_used: int | None
    tokens_delta: int | None = None  # against the first revision; None for it and for unknown usage
    status: str


class TaskComparison(BaseModel):
    task_id: str
    entries: list[TaskEntry]


class Comparison(BaseModel):
    suite_id: str
    revisions: list[RevisionSummary]
    tasks: list[TaskComparison]

    def to_markdown(self) -> str:

        def label(r):
            return r or "working tree"

        lines = [
            f"# Eval `{self.suite_id}`",
            "",
            "| revision | passed | pass rate | total tokens | mean tokens |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {label(r.revision)} | {r.passed}/{r.total} | {r.pass_rate:.0%} "
            f"| {r.total_tokens} | {r.mean_tokens:.0f} |"
            for r in self.revisions
        ]
        header = "| task | " + " | ".join(label(r.revision) for r in self.revisions) + " |"
        lines += ["", header, "|---|" + "---|" * len(self.revisions)]
        for task in self.tasks:
            cells = []
            for e in task.entries:
                tokens = "?" if e.tokens_used is None else str(e.tokens_used)
                delta = "" if e.tokens_delta is None else f" ({e.tokens_delta:+d})"
                cells.append(
                    f"{'pass' if e.passed else 'FAIL'} {tokens}{delta}" + ("" if e.passed else f" [{e.status}]")
                )
            lines.append(f"| {task.task_id} | " + " | ".join(cells) + " |")
        return "\n".join(lines) + "\n"


def compare(results: list[SuiteResult]) -> Comparison:
    """Pass rate, token totals and per-task token deltas (against the first result) across revisions."""
    if not results:
        raise ValueError("nothing to compare")
    if len({r.suite_id for r in results}) != 1:
        raise ValueError("results are from different suites")
    summaries = [
        RevisionSummary(
            revision=r.revision,
            passed=r.passed,
            total=len(r.tasks),
            pass_rate=r.pass_rate,
            total_tokens=r.total_tokens,
            mean_tokens=r.total_tokens / len(r.tasks) if r.tasks else 0.0,
        )
        for r in results
    ]
    by_task = [{t.task_id: t for t in r.tasks} for r in results]
    task_ids = list(dict.fromkeys(i for tasks in by_task for i in tasks))
    comparisons = []
    for task_id in task_ids:
        base = by_task[0].get(task_id)
        entries = []
        for r, tasks in zip(results, by_task, strict=True):
            t = tasks.get(task_id)
            if t is None:
                entries.append(TaskEntry(revision=r.revision, passed=False, tokens_used=None, status="missing"))
                continue
            delta = (
                None
                if t is base or base is None or base.tokens_used is None or t.tokens_used is None
                else t.tokens_used - base.tokens_used
            )
            entries.append(
                TaskEntry(
                    revision=r.revision, passed=t.passed, tokens_used=t.tokens_used, tokens_delta=delta, status=t.status
                )
            )
        comparisons.append(TaskComparison(task_id=task_id, entries=entries))
    return Comparison(suite_id=results[0].suite_id, revisions=summaries, tasks=comparisons)
