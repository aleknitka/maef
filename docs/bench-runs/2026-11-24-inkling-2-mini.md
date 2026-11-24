# Benchmark run, 2026-11-24: Inkling 2 mini on the noisy bowling family

> Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.

This is the first full end-to-end test of `maef bench` on a clean cloud machine. The agent `tdd-engineer` gets the
bowling task with seeded noise (changed pins, frames, bonus rules and names), works in a locked-down sandbox, and is
graded by 45 hidden cases it never sees. How the benchmark works: [BENCHMARKS.md](../BENCHMARKS.md).

## Setup

| | |
|---|---|
| Machine | AWS EC2 t3.small (2 vCPU, 2 GB), Ubuntu 24.04, eu-north-1; access by SSM only, no inbound ports, self-terminates after 4 h |
| Software | repo revision `05ca86d`, Python 3.12.3, rootless Podman 4.9.3 (cgroups v2) |
| Preflight | clean clone from a git bundle, `maef lint`, the 21 real-container tests (sandbox and grader) all passed |
| Agent | `agents/tdd-engineer` (two skills: test-driven development, Python sandbox); tools `run_command`, `read_agent_file` |
| Model | `thinkingmachines/inkling-2-mini` through OpenRouter, `reasoning_effort: low`, max 16,000 output tokens per turn; every call served by PrimeIntellect |
| Budget | 600,000 tokens, 40 turns, 30 minutes per run |
| Recording | event log for every run; full `transcript.jsonl` (prompts, replies, reasoning, tool calls and results) |
| Runs | 9: seeds 1, 2, 3 at noise 0; 3, 4, 5 at noise 1; 7, 8, 9 at noise 2 |

## Results

| seed / noise | variant (pins / frames / strike bonus / spare bonus, names) | time | turns | tokens in (cached) / out (reasoning) | cost | own test runs (R = red, G = green; number = tests ran) | hidden |
|---|---|---|---|---|---|---|---|
| 1 / 0 | 10 / 10 / 2 / 1, `bowling.score` | 34 s | 9 | 39.6k (31.1k) / 13.5k (8.3k) | $0.0103 | R1 G12 G12 | 45/45 |
| 2 / 0 | 10 / 10 / 2 / 1, `bowling.score` | 28 s | 12 | 60.8k (48.8k) / 8.4k (3.3k) | $0.0082 | R1 R2 R2 G2 R11 G0 G11 G11 | 45/45 |
| 3 / 0 | 10 / 10 / 2 / 1, `bowling.score` | 41 s | 11 | 54.3k (47.2k) / 15.8k (10.6k) | $0.0119 | R1 R14 G14 R15 G15 | 45/45 |
| 3 / 1 | 11 / 7 / 2 / 1, `alley.final_score` | 34 s | 8 | 32.1k (23.2k) / 12.1k (7.9k) | $0.0092 | R1 G16 | 45/45 |
| 4 / 1 | 13 / 12 / 2 / 1, `tenpin.tally` | 37 s | 8 | 30.0k (21.9k) / 15.7k (12.2k) | $0.0112 | R1 G14 | 45/45 |
| 5 / 1 | 12 / 6 / 2 / 1, `skittles.final_score` | 76 s | 10 | 58.3k (45.7k) / 12.9k (6.4k) | $0.0056 | R1 G12 R18 G17 | 45/45 |
| 7 / 2 | 6 / 6 / 3 / 2, `alley.final_score` | 58 s | 13 | 85.0k (69.6k) / 17.2k (10.1k) | $0.0075 | R1 R15 R15 R15 G15 | **44/45** |
| 8 / 2 | 5 / 8 / 1 / 2, `skittles.total` | 97 s | 15 | 82.7k (69.0k) / 23.9k (17.6k) | $0.0092 | R1 R1 R1 G1 G3 R8 G8 G13 G13 G13 | 45/45 |
| 9 / 2 | 5 / 4 / 3 / 2, `alley.total` | 32 s | 9 | 46.3k (36.1k) / 13.2k (7.3k) | $0.0104 | R1 G11 | 45/45 |

**Totals:** 9 runs, all `succeeded`; 404 of 405 hidden cases passed (99.8%); 489,128 input tokens (80% from the
prompt cache), 132,688 output tokens (83,806 of them reasoning); **$0.0835** model cost. The API's Prometheus counters
(`maef_cost_usd_total 0.08354591`, `maef_reasoning_tokens_total 83806`) match the per-run reports. Median 10 turns and
mean 48.5 s per run; median model latency 2.0 s per turn (longest 24.4 s). No errors in the API or worker logs.

## Did it work test-first?

All 9 runs got the verdict **test-first (red→green)**: the test file was written (turn 2–3) before the
implementation (turn 4–6), and the first test run failed (an import error, because the module did not exist yet).

The shape of the work changes with noise:

- **Noise 0 and 1:** mostly one big cycle. About 11–16 tests at once, then the whole implementation, then green. Seed 5
  and seed 3/0 added a second cycle (new tests, red, fix, green).
- **Noise 2 (changed bonus rules):** more iterations. Seed 7 stayed red for three runs before going green. Seed 8 took
  10 test runs and 7 file rewrites, growing from 1 test to 13 in small steps: the closest to textbook TDD in this set.
- Seed 2 has a `G0` (a run that found 0 tests and exited 0). The trace counts it as green, which flatters the run
  slightly; see "Next" below.

## The one failure, and why the hidden tests matter

Seed 7, noise 2 (6 pins, 6 frames, a strike gets 3 bonus balls, last frame strike gets 3 fill balls). The agent's own
15 tests were all green, but hidden case "random game 20" failed:

```
rolls    [6, 4, 1, 0, 3, 2, 2, 6, 6, 2, 4, 6]   expected 59
got      ValueError: roll exceeds pins standing
```

In the last frame: strike (6), then fill balls 2, 4, 6. After 2 and 4 all pins are down again, and rule R7 says the
pins reset "whenever all pins are down (after a strike, or after a fill ball that clears the pins standing)". The
agent only reset after a ball that knocked down all 6 at once (`standing = 6 if r == 6 else standing - r`), so the
third fill ball was rejected. This is the kind of mistake the noise is designed to expose: with 3 fill balls instead
of 2, the familiar version of the rule stops being enough, and the agent's own tests never tried that case.

## Earlier local runs (same day, WSL2)

| run | result | turns | time | cost |
|---|---|---|---|---|
| seed 7 / noise 2, `reasoning_effort: medium` | **failed** `output_truncated`: turn 3 spent all 16,000 output tokens reasoning (42,491 characters, the whole solution in its head) and never called a tool | 3 | 47 s | $0.0050 |
| seed 7 / noise 2, `reasoning_effort: low` + "every turn should end in a tool call" | 45/45, test-first | 10 | 77 s | $0.0063 |
| seed 3 / noise 1, clean-clone rehearsal | 45/45, test-first | 10 | 46 s | $0.0098 |

The failed run was diagnosed entirely from its recorded transcript (the reasoning text and the `max_tokens` stop on
turn 3), which is why the transcript is switched on for every benchmark run.

## Costs

| item | cost |
|---|---|
| Model calls, 9 AWS runs | $0.084 |
| Model calls, 3 local runs | $0.021 |
| EC2 t3.small, about 30 min, plus 20 GB gp3 | about $0.02 |
| S3 and SSM | under $0.01 |
| **Total** | **about $0.13** |

## What is recorded for every run

Each run folder (`bench-results/<run id>/`) holds `task.md` (exactly what the agent was given), `variant.json`,
`cases.json` (the hidden cases), `events.jsonl` (the append-only event log), `report.json` / `report.md`, and
`artifacts/` (the agent's deliverables, its final reply `output.md`, and `transcript.jsonl`). The raw data for this
run was kept outside the repository.

## Next

- Score cycle size, so "13 tests then all the code" ranks below small red→green steps.
- Treat a test run that ran 0 tests as neither red nor green.
- Record the request settings (`reasoning_effort`, temperature) and tool definitions in the event log and transcript,
  and add `maef bench replay <run>` for a turn-by-turn view of a run.
- Repeat with more seeds and a second task family, and compare models on the same seeds.
