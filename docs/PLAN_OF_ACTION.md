# Plan of action

Draft, 2026-11-26. How we carry out [ROADMAP.md](ROADMAP.md). Each phase there becomes a list of work packages
here; each package has an aim, the work, the files it touches and the check that closes it. Readiness findings
from the 2026-11-25 review keep their numbers (R1–R10).

## Overall aims

1. **Trustworthy measurement.** A difference between two arms comes from the factor under test, not from lost runs,
   accounting errors, unrecorded settings or provider routing.
2. **Repeatable experiments.** One spec file and one command produce an experiment; its exported record lets someone
   else rerun it and reach the same conclusion within the reported intervals.
3. **Reusable specialists.** The winning arm becomes a released agent that runs from a clean install and carries a
   link to the evidence that it won.
4. **A codebase others can build on.** uv, ruff and ty clean; CI green on Linux and macOS; docs say only what the
   code does.

## How we work

- **One package, one branch, one or more commits.** Every package ends with tests passing and a commit. Branches merge
  into `main` once their check passes.
- **Delegation:** Sonnet implements, Haiku writes tests and does simple tasks; Opus plans, reviews and runs the checks.
- **Tests first** for every fix from the readiness review: the failing check from the review becomes a test before the
  code changes (property test, chaos script, PoC).
- **No paid model calls** until phase 3. Phases 0–2 are checked against the fake OpenAI-compatible model
  written for the readiness review and moved into `tests/` in package 0.4. Any paid run gets a cost estimate and your go-ahead first.
- **AWS stays ephemeral:** only through `scripts/aws-bench/run.sh`, which deletes everything it creates.
- Sizes: S = under half a day, M = 1–2 days, L = 3–5 days.

## Phase 0: Tooling and CI

**Aim:** anyone can clone or install MAEF, run the checks, and trust that CI means what it says.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 0.1 | uv | `uv.lock`; `[dependency-groups] dev` instead of the `dev` extra; Makefile targets use `uv run`; README, `RUNNING.md`, `BENCHMARKS.md`, `scripts/aws-bench/vm-setup.sh` switched from pip/venv | `pyproject.toml`, `uv.lock`, `Makefile`, docs, scripts | S | `uv sync && uv run pytest` passes from a fresh clone; no `pip install` or `venv` left in docs or scripts |
| 0.2 | ruff | `[tool.ruff]` rules (E, F, I, B, UP, S, BLE at least), format the code in one commit, fix or justify remaining findings | `pyproject.toml`, all Python | S | `ruff check` and `ruff format --check` clean |
| 0.3 | ty | `[tool.ty]` config, fix type errors, per-module ignores only with a comment | `pyproject.toml`, Python | M | `ty check` clean, or remaining errors listed as known false positives of the preview release |
| 0.4 | Test selection and fixtures | `addopts = -m "not oci and not pg and not s3"`; Postgres and S3 fixtures remove their containers, including on failure; move the fake model, chaos and property tests from the review into `tests/` | `pyproject.toml`, `tests/conftest.py`, new tests | S | `uv run pytest` runs unit tests only; no containers left after `make test-pg test-s3` |
| 0.5 | Egress proxy on WSL2 | Find why `test_allowlisted_host_connects_through_proxy_only` gets an empty reply here; fix the code or the test | `egress_proxy.py`, `tests/test_*proxy*` | M | the OCI suite passes on WSL2 and in CI |
| 0.6 | CI | GitHub Actions: ruff, ty, unit tests (ubuntu, macOS; Python 3.11 and 3.12), OCI/pg/s3 suites (ubuntu), Studio and TUI tests, `pip-audit` | `.github/workflows/ci.yml` | M | CI green on a pull request |
| 0.7 | Package (R5) | Agents, profiles and schemas found from a project folder (`MAEF_HOME`, else the current directory, else bundled examples) instead of `parents[3]`; failing commands exit non-zero; LICENSE, metadata, `maef --version`, CHANGELOG | `registry.py`, `cli.py`, `localsetup.py`, `pyproject.toml` | M | `uv build`, then `uv tool install dist/*.whl` in an empty folder: `maef init`, `lint` and a fake-model `run` succeed; `twine check` clean |
| 0.8 | Release | Tag `v0.1.0` after 0.1–0.7 | — | S | tag pushed, CHANGELOG entry |

Detailed steps: [M1_PLAN.md](M1_PLAN.md).

## Phase 1: Runs you can trust

**Aim:** no run is lost, cut short or charged wrongly for reasons unrelated to the agent, and every run ending says
whose fault it was.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 1.1 | Token accounting (R1) | Bound `tokens_used` by the reservation (reject above it, with an event); charge max(recorded, reported) on finish and on lease expiry; tests that kill the admission-check mutants; account period reset command; optional USD cap from reported cost | `main.py`, `ledger.py`, `_sql_*.py`, `cli.py`, tests | M | property test with over-reporting passes on SQLite and Postgres; the ledger.py:216-217 mutants are killed; chaos F4 rejected |
| 1.2 | Worker resilience (R2) | Retry sends with backoff until the lease deadline, heartbeating meanwhile; on loss charge recorded usage; artifact-write failures end the run as an infrastructure loss | `worker.py`, `ledger.py` | M | chaos: 30 s API outage leaves the run `succeeded`; killed worker charged about its recorded usage |
| 1.3 | Sandbox output cap (R4) | Read stdout/stderr as streams with a byte cap; stop the command on overflow; report `truncated` | `sandbox.py`, `tools.py` | S | `yes` in the sandbox returns a capped result; worker memory stays under 200 MB |
| 1.4 | Run endings | Split terminal reasons into agent outcomes and infrastructure losses (`worker_lost`, `provider_error`, `sandbox_error`, `artifact_error`); put the class in the run record and the report | `models.py`, `harness.py`, `worker.py`, `bench/report.py`, contracts | S | every terminal reason maps to exactly one class; reports show losses separately |
| 1.5 | Gateway escaping (R3) | Per-call random delimiter; neutralise closing tags inside agent output | `gateway.py`, tests | S | output containing `</maef-result>` stays inside the block |
| 1.6 | Credentials and fetch (R7) | Worker only resolves allowlisted `env:` names; fetch MCP ships with an explicit host list instead of `*` | `worker.py`, `credentials.py`, `mcp-servers/` | S | a lease naming an unlisted variable is refused; fetch to an unlisted host is blocked |
| 1.7 | Docs match code (R7) | Rewrite the claims in `SECURITY.md` and `BUDGETS_OBSERVABILITY.md` that the code does not meet; describe the DNS-rebinding gap or close it by connecting to the vetted IP | `docs/`, `egress.py` | S | the docs-vs-code table from the review has no mismatches |

## Phase 2: Record every condition, and make them configurable

**Aim:** everything that could change a run is written into its manifest, and every such thing can be set on purpose.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 2.1 | Run manifest | Schema and builder: request settings, tool definitions, rendered prompt and first message, model requested and served (provider, version, route), harness commit, image digest, sandbox profile, host and runtime versions; stored as an artifact and digested into the run record | new `manifest.py`, `harness.py`, adapters, `schemas/`, contracts | M | a changed setting always changes the manifest digest; two fake-model runs with the same inputs have the same digest |
| 2.2 | Served-model capture | Adapters record what the provider reports (OpenRouter `provider`, model id, system fingerprint) per turn | `adapters/*.py` | S | fake-model tests assert the fields; recorded in the transcript |
| 2.3 | Provider pinning | Endpoint profile option for OpenRouter provider order and no fallbacks; a run fails as an infrastructure loss if served by an unpinned provider | `models.py`, `adapters/openai_compatible.py` | S | request body carries the pin; mismatch is detected |
| 2.4 | Model settings per agent | `runtime.model` (`reasoning_effort`, `temperature`, `top_p`, `seed`, `max_output_tokens`, plus a free-form `extra` map) set by the user; nothing is fixed by MAEF. A setting is sent only when set; the manifest records what was sent and what the provider accepted, ignored or rejected (many current APIs refuse temperature or seed for reasoning models); a rejected setting fails the run as a configuration error unless the agent marks it `optional` | `models.py`, `definitions.py`, adapters | M | fake-model tests: unset settings are not sent; a rejected setting is recorded and handled as configured |
| 2.5 | Skills mode and tool settings | `skills_mode: inline | on_demand`; per-agent command timeout and result size within the global limits | `models.py`, `harness.py`, `tools.py` | S | inline mode puts the pinned skill bytes in the system prompt; limits appear in the manifest |
| 2.6 | Variants | `variants:` in `agent.yaml`, selected with `agent@variant`; each variant has its own digest; lint checks them | `models.py`, `definitions.py`, `lint.py`, `cli.py`, API | M | `agent@a` and `agent@b` pin different digests; unknown variants refused |
| 2.7 | Render | `maef agent render <agent>[@variant]` prints the system prompt, first message, tool definitions and model settings | `cli.py`, `definitions.py` | S | output is byte-identical to the first request the fake model receives |
| 2.8 | Replay | `maef bench replay <run>`: turn-by-turn view from events and transcript | `cli.py`, `bench/` | S | replays the recorded AWS runs in `docs/bench-runs/` |

## Phase 3: Experiments

**Aim:** one spec, one command, a complete and fair set of runs.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 3.1 | Spec | YAML schema: question, arms (variants, overrides, or a different harness), fixed factors, task family, noise levels, seeds, repeats, metrics, per-run budget, experiment spending ceiling; lint | `schemas/`, new `experiments/spec.py`, `lint.py` | M | invalid specs refused with a clear message; arms must differ in at least one factor |
| 3.2 | Store | Experiments and their runs in the ledger (experiment id, arm, seed, repeat on each run) on SQLite and Postgres | `ledger.py`, `_sql_*.py` | M | same suite passes on both databases |
| 3.3 | Scheduler | Expand the spec, shuffle with a recorded seed, interleave arms, re-run infrastructure losses up to a limit, resume after a stop | new `experiments/run.py` | M | stopped and resumed experiment ends with exactly the spec's run list |
| 3.4 | Cost guard | Estimate from the model's prices and past runs; confirm before starting; stop at the ceiling | `experiments/run.py`, `cli.py` | S | a fake-model experiment stops at a low ceiling |
| 3.5 | CLI and API | `maef experiment run | status | report`; API routes; TUI list | `cli.py`, `main.py`, `apps/tui` | M | a two-arm fake-model experiment runs end to end |
| 3.6 | External harness arms | A model proxy all harnesses must use (budget, tokens, transcript), a harness seam (`runtime.harness`), and adapters for **LangChain Deep Agents** and **AWS Strands Agents** running inside the sandbox. Packages H.1–H.6 in [HARNESSES.md](HARNESSES.md); H.1–H.4 can start after phase 1, alongside phase 2 | `apps/api/maef_api/proxy.py`, `worker.py`, new `harnesses/` | L | the same experiment runs under MAEF's loop, Deep Agents and Strands, with comparable manifests and metrics |
| 3.7 | First real experiment | `reasoning_effort` low vs medium on Inkling 2 mini, 5 seeds × 3 repeats (about $0.50; needs your go-ahead) | `experiments/` example spec | S | report published in `docs/experiments/` |

## Phase 4: Measurement and statistics

**Aim:** every number in a report has a versioned definition and an honest interval.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 4.1 | Metric registry | Metric name, version, group and function; reports list metric versions; re-score command | new `metrics_registry.py` (name to settle against existing `metrics.py`), `bench/report.py` | M | changing a metric version re-scores old runs without re-running |
| 4.2 | Metrics | outcome (hidden pass rate, by noise level), process (test-first, cycle size, 0-test runs neither red nor green), efficiency (tokens, reasoning share, cost, turns, wall time, latency), safety (blocked commands, network attempts, budget hits) | `bench/trace.py`, registry | M | each metric has a test on a recorded run |
| 4.3 | Statistics | Paired-by-seed differences, bootstrap intervals, effect sizes, spread across repeats; "within noise" stated when the interval covers zero | new `experiments/stats.py` | M | checked against a hand-computed example and scipy on synthetic data |
| 4.4 | Report | Markdown and JSON experiment report: per-arm table, paired differences, losses, cost | `experiments/report.py` | S | the phase 3.7 experiment re-reported with intervals |
| 4.5 | Second task family | A family unlike bowling (for example a text-parsing or refactoring task) with noise and hidden cases | `bench/families/` | L | 45 hidden cases, noise levels 0–2, grader tests |

## Phase 5: FAIR records

**Aim:** an experiment can be found, opened, understood and rerun by someone else.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 5.1 | Export | `maef experiment export` writes a bundle: spec, manifests, events, transcripts, artifacts, metrics, report | new `experiments/export.py` | M | bundle opens without MAEF (plain files, documented layout) |
| 5.2 | RO-Crate and PROV | `ro-crate-metadata.json` with PROV relations (spec → runs → metrics → report); licence and citation metadata | `experiments/export.py` | M | passes an RO-Crate validator |
| 5.3 | Traces | OTel export follows the GenAI semantic conventions | `otel.py` | S | attribute names match the conventions; tested |
| 5.4 | Index | Search experiments and runs by agent, model, task family, metric and date (CLI and API) | `ledger.py`, `cli.py`, `main.py` | S | queries return the expected experiments |
| 5.5 | Rerun | `maef experiment rerun <bundle>` reproduces the experiment from the bundle | `experiments/` | M | the phase 3.7 bundle rerun on a fresh AWS machine reaches the same conclusion within intervals (paid; needs go-ahead) |

## Phase 6: From experiment to specialist

**Aim:** the best agent found by experiment is released, runnable anywhere, and protected against regressions.

| # | Package | Work | Files | Size | Closed when |
|---|---|---|---|---|---|
| 6.1 | Promote | `maef agent promote <experiment> <arm>`: writes the arm as the agent's released revision, git tag, link to the experiment record | `cli.py`, `drafts.py`, `gitrepo.py` | M | tag points at the arm's digest; release notes link the record |
| 6.2 | Specialist bundle | Package a released agent (definition, skills, pinned profiles and image digests) and run it on another install | new `bundle.py`, `cli.py` | M | bundle runs from a clean `uv tool install` |
| 6.3 | Regression gate | A new revision must not do worse than the released one on its experiment's metrics (within intervals) | `experiments/`, `cli.py` | M | a deliberately worse revision is refused |
| 6.4 | Unattended hardening (R8) | Token expiry and rate limits; re-check tokens on open event streams; bounded audit rows for rejected requests; strict Bearer parsing; https away from localhost | `auth.py`, `main.py`, `ledger.py` | M | auth mutation score at least 80%; the flood PoC stays bounded |

## Later (not scheduled)

R9 (SSE capacity, shutdown, pagination, Postgres pool), R10 (readiness check, backups, schema version, DB file mode,
worker container labels), remote workers (backend step 13), decision models (backend step 9). They move up if MAEF
becomes a shared lab.

## Order and milestones

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6
 0.1–0.4 first        1.1–1.4 before 2.x       3.1–3.5 need 2.1, 2.6   4.1–4.4 need 3.x
 0.5–0.7 in parallel  1.5–1.7 in parallel      2.x mostly in parallel   4.5 can start any time
```

| Milestone | After | Shows |
|---|---|---|
| M1 `v0.1.0` | phase 0 | installs cleanly; CI green |
| M2 trustworthy runs | phase 1 | chaos and property checks from the review all pass |
| M3 reproducible runs | phase 2 | manifest digest per run; render and replay |
| M4 first experiment | phases 3 and 4 | a real two-arm experiment with intervals (first paid step) |
| M5 FAIR | phase 5 | an exported bundle rerun elsewhere reaches the same conclusion |
| M6 specialist | phase 6 | a promoted agent runs from a clean install with its evidence |

## Risks

| Risk | Effect | Response |
|---|---|---|
| ty is a preview release | false positives block CI | run it as a warning; list known false positives |
| Provider nondeterminism even with a fixed seed | repeats differ | measure spread with repeats; never compare single runs |
| OpenRouter can't pin every provider | arms served differently | record the served provider per turn; drop or re-run mismatched runs |
| WSL2 differs from CI Linux | proxy and podman issues hidden locally | CI runs the OCI suites; macOS runner for podman machine |
| Paid runs grow with seeds × repeats × arms | cost | cost estimate and ceiling per experiment; your go-ahead before each |

## Decisions

Settled 2026-11-26:

- Licence: free private and business use; the name protected, not renaming of forks (an open-source licence can't
  forbid that). Recommended: Apache-2.0 + NOTICE + TRADEMARKS.md; see [M1_PLAN.md](M1_PLAN.md).
- GitHub Actions: yes, on the free plan; the repository is public, so standard runners are free. CI stays frugal
  (see M1_PLAN.md).
- Model settings: users set them; MAEF fixes nothing and records what was sent and what the provider accepted (2.4).
- Experiments may swap the harness (3.6).

Still open: the name for NOTICE; the distribution name; the first paid experiment (3.7, about $0.50).
