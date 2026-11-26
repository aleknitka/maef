# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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

### Changed

- Tooling moved to uv (`uv sync`, `uv run`, a committed `uv.lock`), ruff for lint and format, ty for type checks.
- The `maef` command exits 1 on any failure (message on stderr), 2 on a usage error and 3 on a timeout. It used to
  exit 2 for API errors.
- `maef init` no longer reports success when it fails.
- Benchmark reports record the installed harness version alongside the git revision.

## [0.1.0] - unreleased

First release, to be dated when tagged. What exists:

- Agents defined in files (instructions, skills, tools, budget), pinned by git revision and file digest for every run.
- A FastAPI service, a separate worker, a SQLite or Postgres ledger and local or S3-compatible result storage.
- Model adapters for Ollama, LM Studio, OpenAI, Anthropic and AWS Bedrock, and decision models (Jev, Clef).
- Token budgets reserved before a run; per-run and account-wide limits.
- Podman or Docker sandboxes, MCP tool servers behind an egress allowlist, and the `maef mcp` gateway.
- Scoped API tokens, worker credentials, an audit log, git-backed drafts for editing agents.
- OpenTelemetry export, Prometheus metrics, golden-task evals and the `maef bench` benchmarks.
- A terminal interface (`apps/tui`) and a read-only web Studio (`apps/studio`).
