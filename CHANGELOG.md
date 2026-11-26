# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Phase 1 of the roadmap: runs you can trust.

### Fixed

- Token accounting (R1): every settle path, including lease expiry, charges min(max(reported, recorded),
  reservation); over-reports are clamped and recorded as `budget.overreport`; `tokens_used` is bounded.
- Worker outages (R2): the worker retries reports and heartbeats with capped, jittered backoff until its lease
  deadline, and stops at once when the run is no longer its own; artifact store failures end the run as
  `artifact_error` instead of crashing the worker.
- Gateway output (R3) is wrapped in a block with a per-call random delimiter; `maef-result` tags inside it are
  neutralised.
- Sandbox output (R4) is streamed with a per-stream byte cap, and the command is stopped when it overflows.
- Worker credentials (R7): only names referenced by local model profiles or MCP manifests, or listed in
  `MAEF_WORKER_ALLOWED_CREDENTIALS`, are resolved; a profile's key works only with that profile's endpoint; an MCP
  server's secrets are passed only when the lease's manifest matches the worker's local copy.
- `docs/SECURITY.md` and `docs/BUDGETS_OBSERVABILITY.md` describe what the code does, with known gaps listed.

### Added

- Run endings: every terminal reason is an agent outcome or an infrastructure loss (`ending` on the run record);
  bench reports leave losses out of the score. Model endpoint failures end as `provider_error`.
- `maef admin account reset` starts a new budget period (audited).
- The fetch MCP server ships an explicit host list; `maef lint` warns on `*` in an allowlist.

### Changed

- A worker without a project folder needs `MAEF_WORKER_ALLOWED_CREDENTIALS` to resolve any `env:` credential.

## [0.1.0] - 2026-11-26

First release. What exists:

- Agents defined in files (instructions, skills, tools, budget), pinned by git revision and file digest for every run.
- A FastAPI service, a separate worker, a SQLite or Postgres ledger and local or S3-compatible result storage.
- Model adapters for Ollama, LM Studio, OpenAI, Anthropic and AWS Bedrock, and decision models (Jev, Clef).
- Token budgets reserved before a run; per-run and account-wide limits.
- Podman or Docker sandboxes, MCP tool servers behind an egress allowlist, and the `maef mcp` gateway.
- Scoped API tokens, worker credentials, an audit log, git-backed drafts for editing agents.
- OpenTelemetry export, Prometheus metrics, golden-task evals and the `maef bench` benchmarks.
- A terminal interface (`apps/tui`) and a read-only web Studio (`apps/studio`).

Release engineering in this version:

- Apache-2.0 licence, NOTICE and TRADEMARKS.md.
- Packaging as a wheel and sdist (hatchling). The starter agents, sandbox and model-profile examples, MCP server
  manifests, schemas and evals ship inside the wheel; `scripts/check-wheel.sh` builds it, installs it into a throwaway
  tool directory and runs `maef init`, `maef lint` and a run against a fake model.
- `maef --version`, `maef api` and `maef worker`.
- `maef init` in an empty folder creates a project: starter files, `maef.toml`, a git repository with the starter
  files committed (agent definitions are pinned by git revision), then the model profile, results folder and database.
- `project_root()`: the project is `MAEF_HOME`, else the nearest folder above the current one with a `maef.toml`, else
  the source checkout.
- CI on GitHub Actions: lint, types, unit tests on Python 3.11 to 3.13, container suites, TUI tests, Studio build,
  dependency audit, package check, and a weekly macOS run.
- Test fixtures: a fake OpenAI-compatible model server, labelled test containers that are always removed, a budget
  property test, and strict expected failures for the known budget and worker-outage bugs.
- Tooling moved to uv (`uv sync`, `uv run`, a committed `uv.lock`), ruff for lint and format, ty for type checks.
- The `maef` command exits 1 on any failure (message on stderr), 2 on a usage error and 3 on a timeout. It used to
  exit 2 for API errors.
- `maef init` no longer reports success when it fails.
- Benchmark reports record the installed harness version alongside the git revision.

Known issues (from the 2026-11-25 readiness review; fixes planned in `docs/ROADMAP.md`, phase 1):

- A worker can report more tokens than its run reserved, and an expired lease is charged only the reservation (R1).
- A worker abandons its run when the API is unreachable for more than about 3 seconds (R2).
- Agent output returned through the gateway is not escaped (R3); sandbox command output is buffered in full before
  it is cut to size (R4).
- `docs/SECURITY.md` and `docs/BUDGETS_OBSERVABILITY.md` claim some protections the code does not provide yet (R7).
