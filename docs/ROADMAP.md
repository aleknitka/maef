# Roadmap

Agreed 2026-11-25. This replaces the original engineering slices: most of what they described is built (see
[BACKEND_PLAN.md](BACKEND_PLAN.md) for steps 0–14). The open findings come from the readiness review of 2026-11-25
(commit `a50ff85`); its numbers are kept below as `R1`–`R10`.

## What MAEF is

**MAEF is a test bed for controlled, repeatable experiments on AI agents.** You change one thing (model, prompt,
skills, tools, settings), keep everything else fixed, run each version in a sealed environment across many seeds,
and measure the result, the way the agent worked, cost and safety. Every experiment is saved as a FAIR record
(Findable, Accessible, Interoperable, Reusable) that someone else can find, check and run again.

It is a data-science project: the aim is to optimise agents (their prompts, skills, tools and model settings) by
experiment rather than by feel.

The experiments have a second output. The agent definitions they produce (instructions, skills, tool grants, model
and sandbox profiles, budgets) are self-contained. **The winning version of an agent can be released and run as a
specialist on its own**, from the `maef` CLI, from Claude Code or Codex through the gateway, or packaged as a bundle,
carrying the evidence that it won.

```
define arms ──► experiment (seeds × repeats × arms) ──► sealed runs ──► metrics + statistics ──► FAIR record
                                                                                         │
                                                       promote the winning arm ◄─────────┘
                                                                │
                                                                ▼
                                            released specialist agent (CLI, gateway, bundle)
```

## What already exists

| Need | Built |
|---|---|
| Fixed inputs | agent files pinned by git digest; sandbox images pinned by digest; budget limits per run |
| Sealed environment | podman sandbox: no network, read-only root, all capabilities dropped, memory/CPU/process caps |
| Tasks the agent hasn't memorised | `maef bench` task families with seeded noise; hidden grading cases in a separate container |
| Recording | append-only event log; full transcript (prompts, replies, reasoning, tool calls and results) |
| Measurements | tokens (cached, reasoning), cost, turns, latency, hidden pass rate, TDD trace |
| Reuse as specialists | worker + leases, MCP tools, artifact store, `maef` CLI and the Claude Code / Codex gateway, TUI |

## Phases

Each phase ends with a check that proves it is done. Phases run in order; items inside a phase can go in parallel.

### Phase 0: Tooling and CI

- Move to **uv** (`uv sync`, `uv run`, `uv build`, committed `uv.lock`), **ruff** (lint and format, config in
  `pyproject.toml`) and **ty** (type checking). Update the README, `RUNNING.md` and the Makefile; no `pip install`
  or `venv` instructions left.
- Plain `pytest` runs unit tests only (`addopts = -m "not oci and not pg and not s3"`); container suites run on
  demand. Test fixtures remove the Postgres and S3 containers they start.
- GitHub Actions: ruff, ty, unit tests (Linux and macOS), container suites (Linux), Studio/TUI tests, `pip-audit`.
- Find out why the egress-proxy tests fail on WSL2, so the network allowlist is proven, not assumed (R6).
- A working package (R5): agents, profiles and schemas ship as package data or are found from a project folder;
  failing commands exit non-zero; LICENSE, package metadata, `maef --version`, CHANGELOG, tag `v0.1.0`.

**Done when:** CI is green including the container suites; `uv tool install` of the built wheel in an empty folder
can `init`, `lint` and `run`; `ruff check`, `ruff format --check` and `ty check` are clean.

### Phase 1: Runs you can trust

A comparison is only fair if no run is lost, cut short or charged wrongly for reasons that have nothing to do with
the agent.

- **Token accounting (R1):** reject or clamp reported usage above the reservation (with an event); charge the larger
  of recorded and reported usage everywhere, including lease expiry; tests that catch the surviving mutants in the
  admission check; optional period reset and dollar cap.
- **Worker survives API outages (R2):** retry with backoff until the lease deadline while heartbeating; a lost run is
  charged its recorded usage, not the full reservation.
- **Sandbox output cap (R4):** stream command output with a byte limit and stop the command on overflow.
- **Classify every ending:** each run ends as an agent outcome (`succeeded`, `failed`, `budget_exhausted`, …) or an
  infrastructure loss (`worker_lost`, `provider_error`, …). Infrastructure losses are re-run, never scored.
- **Docs match the code (R7):** correct `SECURITY.md` and `BUDGETS_OBSERVABILITY.md`; the fetch MCP gets an
  explicit allowlist instead of `*`; the worker only resolves allowlisted credential names.
- **Gateway escaping (R3):** a per-call random delimiter around agent output, and closing tags inside it neutralised.

**Done when:** the budget property test passes with over-reporting switched on; a 30-second API outage leaves the run
`succeeded`; `yes` in the sandbox returns a capped result with worker memory under 200 MB; agent output containing
`</maef-result>` stays inside the block.

### Phase 2: Record every condition, and make them configurable

A run can't be reproduced or compared unless everything that could affect it is written down.

- **Run manifest**, saved with every run and covered by its digest:
  - request settings: temperature, top_p, seed, `reasoning_effort`, output-token limit;
  - the exact tool definitions sent to the model, and the rendered system prompt and first message;
  - the model as requested and as served (provider, model version, OpenRouter route);
  - harness commit, sandbox image digest, sandbox profile, host OS, runtime and versions.
- **Provider pinning:** an endpoint profile can fix the provider (for example OpenRouter `provider.order` with
  fallbacks off), so one arm isn't silently served by different hardware.
- **Config composition** (how arms are defined):
  - `maef agent render <agent>[@variant]` prints exactly what the model will receive;
  - `skills_mode: inline | on_demand` (skills put in the prompt, or read with `read_agent_file` as now);
  - `runtime.model` settings (reasoning effort, temperature, output limit) that can only narrow the profile;
  - `variants:` in `agent.yaml`, chosen with `agent@variant`, each with its own digest;
  - per-agent tool settings (command timeout, result size), now fixed at 30 s and 32 KiB.
- `maef bench replay <run>`: a turn-by-turn view of a recorded run.

**Done when:** two runs with identical manifests differ only in model randomness, and a changed setting always
changes the manifest digest; `maef agent render` output is byte-identical to the first request the model gets.

### Phase 3: Experiments

- **Experiment spec** (a YAML file in git): the question; the arms (agent variants or overrides); the factors held
  fixed; the task family, noise levels and seeds; repeats per seed; the metrics; the budget per run (the same for
  every arm).
- `maef experiment run | status | report <spec>`: expands the spec into runs, runs them in random, interleaved order
  (so time-of-day or provider load doesn't favour one arm), re-runs infrastructure losses, and can resume after a
  stop.
- Experiments and runs get stable IDs; each run records which experiment, arm, seed and repeat it belongs to.
- Cost estimate and confirmation before an experiment starts, and a hard spending ceiling for the experiment.

**Done when:** a two-arm experiment (for example `reasoning_effort` low vs medium, 5 seeds × 3 repeats) runs end to end
from one command, survives being stopped and resumed, and its run list matches the spec exactly.

### Phase 4: Measurement and statistics

- **Metric registry:** each metric has a name, a version and a definition in code; reports state the metric versions
  they used; old runs can be re-scored when a definition changes.
- **Metrics in four groups:**
  - outcome: hidden pass rate, pass rate by noise level (robustness);
  - process: test-first verdict, cycle size, test runs that ran 0 tests counted as neither red nor green;
  - efficiency: tokens, reasoning share, cost, turns, wall time, model latency;
  - safety: blocked commands, network attempts, budget hits, prompt-injection exposure.
- **Statistics:** comparisons paired by seed; bootstrap confidence intervals; effect sizes; variance across repeats
  shown, not hidden. A report says when a difference is within noise.
- More task families than bowling, so results don't depend on one kind of task.

**Done when:** an experiment report shows each metric per arm with intervals and the paired difference, and
re-scoring a finished experiment after a metric change produces a new report without re-running anything.

### Phase 5: FAIR records

- **Findable:** an index of experiments and runs searchable by agent, model, task family, metric and date.
- **Accessible:** `maef experiment export` writes a self-contained bundle in open formats (manifests, events,
  transcripts, artifacts, metrics, report).
- **Interoperable:** JSON Schema for manifests and specs; traces follow OpenTelemetry's GenAI conventions; the bundle
  is an RO-Crate with W3C PROV provenance.
- **Reusable:** a licence and citation metadata on every bundle, and `maef experiment rerun <bundle>` repeats it on
  another machine.

**Done when:** a bundle exported here and rerun on a clean machine (for example with `scripts/aws-bench/run.sh`)
reproduces the original conclusions within the reported intervals, and the bundle passes RO-Crate validation.

### Phase 6: From experiment to specialist

- `maef agent promote <experiment> <arm>`: turns the winning arm into a released agent revision (a git tag) that
  links to the experiment record that justified it.
- A released agent runs as a self-contained specialist: from the CLI, through the gateway, or packaged as a bundle
  (definition, skills, pinned profiles and images) that runs anywhere MAEF is installed.
- Regression gate: a new revision of a released agent must not do worse than the current one on its experiment's
  metrics before it is promoted.
- Hardening needed once specialists run unattended or for others (R8): token expiry and rate limits, re-checking
  tokens on open event streams, bounded audit rows for rejected requests, https away from localhost.

**Done when:** a promoted agent runs from a clean install, and its release links to the evidence; a worse revision is
refused by the regression gate.

## Later

- API capacity and shutdown under many live streams (R9); readiness check, backups, schema version, worker container
  labels (R10). These matter for a shared, always-on service more than for a single-user lab.
- Remote ephemeral workers (backend step 13) for experiments larger than one machine.
- Decision models (backend step 9).

## Open questions

- Should arms also be able to swap the harness (an outside agent framework in the same sandbox and grading), or only
  models, prompts, skills, tools and settings?
- One researcher on one machine, or a shared lab with several people and remote workers? This decides when R8–R10
  move up.
- Is there a target venue for FAIR records (a paper, a public results archive) whose format we should match?
- ty is still a preview release; if it blocks CI on valid code, run it as a warning until it stabilises.
