# Benchmarks

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

## Why

Famous coding tasks (katas, public benchmarks) end up in every model's training data. An agent that solves them may
only be remembering the answer. We want to measure how an agent *works through a task it has not seen*.

So we do not ship fixed tasks. We ship **task families**. A family is three things:

- a task template (the text the agent reads),
- a parameter space (numbers, rules and names that can change),
- a reference solution (the truth for every variant).

A **seed** picks one variant. A **noise level** says how far to move away from the well-known task. The hidden
acceptance cases are regenerated from the reference for that exact variant, so a memorised answer to the public task
fails.

## Task families and noise levels

Today there is one family: `bowling`, the classic TDD bowling-score kata.

| noise | what changes |
|---|---|
| 0 | The canonical kata: 10 pins, 10 frames, strike bonus 2 rolls, spare bonus 1 roll, module `bowling`, function `score`. |
| 1 | Number of pins (5 to 15), number of frames (4 to 12), and the module and function names. |
| 2 | Also how many bonus rolls a strike (1 to 3) and a spare (1 to 2) earn. |

The task text states the rules, including how pins reset in the last frame. The same seed and noise always give the
same variant and the same hidden cases.

## What is measured

**Hidden acceptance.** About 45 cases (gutter game, all ones, spares, strikes, perfect game, random valid games,
and 8 invalid inputs that must raise `ValueError`). They run in a locked-down container (no network, read-only,
unprivileged user, 120 s limit) against the files the agent produced. A crash, a wrong answer or a missing module
just counts as failed cases.

**TDD trace.** Read from the run's `transcript.jsonl`, using only `run_command` calls:

- *file write*: a command that writes a path (`cat > file`, `tee`, `python -c open(...,'w')`, `sed -i`). A file named
  `test_*.py` is a test; other `.py` files are implementation.
- *test run*: a command with `unittest` or `pytest`, or running a `test*.py` file. Results are read from
  `Ran N tests`, `OK`, `FAILED (failures=a, errors=b)` (or pytest's summary).
- *tests written first*: the first test-file write comes before the first implementation write.
- *red*: the first test run failed. *green*: a run passed. *red to green cycle*: a failed run followed by a passing one.
- Verdict: `test-first (red→green)`, `test-first (never red)`, `tests after code`, or `no tests`. Rare cases are
  `test-first (red, never green)` and `test-first (tests never run)`.
- Also: turns to first green, whether the final run was green, and how often each file was rewritten.

**Cost, tokens, latency.** Per turn and in total: input, cached, output and reasoning tokens, latency, and cost in
USD when the provider reports it (otherwise "not reported"). Wall time comes from event timestamps.

The report also lists which deliverables are present or missing, the agent's own last test result, and the variant
parameters next to the canonical ones.

## How to run

```bash
# See the task for one variant (add --json to see the variant and hidden cases)
maef bench variant --family bowling --seed 7 --noise 2

# Run an agent on it, wait, grade, and write the report
maef bench run --family bowling --seed 7 --noise 2 --agent tdd-engineer --out bench-results --timeout 1800

# Re-grade a run that already exists
maef bench report <run_id> --family bowling --seed 7 --noise 2 --out bench-results
```

The API address and token come from `MAEF_API_URL` and `MAEF_API_TOKEN`, like the other commands. Grading needs
podman or docker on the machine running `maef bench`, and the pinned image from
`sandbox-profiles/python-sandbox.yaml`.

Everything goes to `bench-results/<run_id>/` (git-ignored): `task.md`, `variant.json`, `cases.json`, `events.jsonl`,
`artifacts/` (`output.md`, `transcript.jsonl`, the agent's files), `report.json`, `report.md`.

Use the same seed and noise to compare agents or models fairly. Use several seeds to see the spread.

## How to add a family

1. Create `apps/api/maef_api/bench/families/<name>.py` with a class that has an `id` and these methods:
   `sample(seed, noise) -> Variant`, `render_task(v)`, `hidden_cases(v)`, `grader_source(v)`. Optionally
   `deliverables(v)` returns the file names the task asks for.
2. Write a reference solution and compute every expected value from it. Add a `_self_check()` that asserts
   well-known canonical answers and that the generator only produces valid inputs.
3. `grader_source` returns a stdlib-only script. It imports the agent's code from `/work`, reads `/grader/cases.json`,
   prints one JSON line `{"passed", "failed", "errors": [{case, expected, got}]}`, and never crashes.
4. Register it in `FAMILIES` in `families/__init__.py`.

Keep noise level 0 equal to the well-known task, so you can compare memorised and unseen performance.

## Limits

- Command parsing is a heuristic (regular expressions over the command text). Unusual ways of writing files or
  running tests can be missed. Treat the verdict as a strong hint, not a proof.
- The TDD verdict says nothing about test quality. The hidden cases test the code, not the agent's tests.
- Only one family exists so far, so results say little about general ability.
- The transcript, usage and cost fields depend on the harness and provider; absent values show as "-" or "not reported".

## Recorded runs

- [2026-11-24: Inkling 2 mini, 9 runs on AWS](bench-runs/2026-11-24-inkling-2-mini.md): 404/405 hidden cases, all test-first, $0.08
