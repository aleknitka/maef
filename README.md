<p align="center">
  <img src="assets/maef-logo.png" alt="MAEF — Modular Agent Execution Framework" width="860">
</p>

# MAEF — Modular Agent Execution Framework

> **Intelligence is not what you do when you know what to do, but what you do when you don't know what to do.**
> <sub>after Jean Piaget — the idea behind how MAEF tests agents ([benchmarks](docs/BENCHMARKS.md))</sub>

MAEF runs **specialist AI agents** for you, safely and within a budget, on your own machine.

You describe an agent once in a few text files (what it is for, its instructions, which model it uses, which tools
it may touch, how many tokens it may spend). Then you, a script, or another coding assistant such as Claude Code
can hand it a task. MAEF:

- **checks the request** and reserves a token budget before anything runs,
- **runs the agent** in a separate worker process, with its tools locked inside containers,
- **records everything** that happens (each model call, tool call and token spent) in an append-only log,
- **stores the result** as a file you can download and check.

Everything works **offline-first**: the database is a file, results go into a local folder, models can come from
Ollama or LM Studio, and containers run in Podman. Cloud services (OpenAI, Anthropic, AWS Bedrock, S3, Postgres) are
optional extras you switch on when you want them.

![How MAEF fits together](docs/diagrams/design/architecture-dark.svg)

## Quick start (no cloud account)

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you), [Podman](https://podman.io) (or Docker) and a local model server
([Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai)).

```bash
uv sync                             # creates .venv and installs the `maef` command
ollama pull qwen3:8b                # any model that supports tool calling
uv run maef init                    # sets up the local model profile, results folder and database
```

Commands below are written as `maef ...`; inside the checkout run them as `uv run maef ...`. For a global `maef`
command that works from any folder, run `uv tool install .` once.

`maef init` creates three things, all on this machine:

| What | Where | Why |
|---|---|---|
| Model profile | `model-profiles/local-default.yaml` | tells agents which model server to call |
| Results folder | `artifacts/` | every result file, named by its fingerprint (sha256) |
| Database | `runs/maef.db` | the log of every run, event and token spent |

Use `maef init --backend lmstudio --model-id <name>` for LM Studio. No local model? `maef init --backend openrouter`
uses a free hosted model through [OpenRouter](https://openrouter.ai) (export `OPENROUTER_API_KEY` first; prompts then
leave your machine). On **WSL2** with the model server running on
Windows, add `--base-url http://<windows-ip>:11434/v1` (find the IP with `ip route | awk '/default/ {print $3}'`).

Now create access keys and start the two processes:

```bash
export MAEF_API_TOKEN=$(maef admin token create --name me --scopes read,runs:write)
export MAEF_WORKER_CREDENTIAL=$(maef admin worker create --id worker-1)

MAEF_EXECUTION_ENABLED=1 MAEF_ALLOW_DIRTY=1 make api    # terminal 1: the API
make worker                                             # terminal 2: the worker (same exports)
```

And give an agent a task:

```bash
maef run research-analyst "Summarise what HTTP caching headers do" --wait
```

Prefer a screen? Run `make tui` for the terminal interface (needs [Bun](https://bun.sh); `cd apps/tui && bun install`
once).

## Install from a release

Download the wheel from the release page, then install it as a tool and set up a project in an empty folder:

```bash
uv tool install maef_control_plane-0.1.0-py3-none-any.whl
mkdir my-agents && cd my-agents
maef init                           # starter agents, a git repository, model profile, results folder, database
maef lint
```

`maef init` needs `git` (agent definitions are pinned by git revision). It writes `maef.toml`, which marks the folder
as a project: run `maef` from it or any subfolder, or set `MAEF_HOME`. Then follow the access-key steps above, using
`MAEF_EXECUTION_ENABLED=1 maef api` and `maef worker` in place of `make api` and `make worker`.

## The main ideas

| Term | Meaning |
|---|---|
| **Agent** | A folder in `agents/` with `agent.yaml` (settings) and instruction files. Kept in Git, so every change is reviewable. |
| **Run** | One task given to one agent. It has a status, a budget, an event log and result files. |
| **API** | The "front desk". It checks requests, reserves budgets, keeps the log and hands out work. It never runs agent code itself. |
| **Worker** | The process that actually runs agents. It asks the API for work, calls the model, runs tools, and reports back. |
| **Model profile** | Which model server to call and how (`model-profiles/*.yaml`). Kept per machine, never committed, never holds secrets. |
| **Sandbox** | A locked-down container for running commands: read-only, no network, no special permissions. |
| **MCP server** | A packaged tool (for example "fetch a web page") that runs in its own container, with internet access only through a filtering proxy. |
| **Budget** | Token limits per run (plus turn and time limits). The worst case is reserved up front; unused tokens are returned. |
| **Artifact** | A result file, stored under its sha256 fingerprint and checked every time it is read. |

## What it can do today

- Run agents against **Ollama, LM Studio, OpenAI, Anthropic and AWS Bedrock**, with tool calling.
- Ask **decision models** (Jev, Clef) structured questions with checked answers.
- **Token budgets** with up-front reservation, per-run limits and an account-wide cap.
- **Container sandboxes** on Podman or Docker (Linux, WSL2, macOS), plus MCP tool servers behind an egress allowlist.
- **Live progress** (server-sent events), cancellation, and downloadable results.
- **Use from other assistants**: Claude Code or Codex delegate tasks through the `maef mcp` gateway, without ever
  loading the agent's tools or secrets.
- **Safe editing of agents**: drafts live on their own Git branch; a human merges them.
- **Access control**: scoped API tokens, one credential per worker, an audit log.
- **Monitoring**: OpenTelemetry traces, Prometheus metrics, and repeatable evaluation suites. Per call: tokens
  (cached and reasoning included), latency, provider and reported cost; optionally a full transcript of the run.
- **Benchmarks that resist memorisation** (`maef bench`): well-known tasks with seeded noise (changed rules, numbers
  and names), graded by hidden tests, with a test-driven-development trace of how the agent worked.
- **Storage choices**: SQLite or Postgres for the log; a local folder or S3-compatible storage for results.

Not done yet: workers on a *second* machine (planned next), and the web Studio still lacks login support.

## Documentation

| Read this | To learn |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit together, with diagrams |
| [docs/RUNNING.md](docs/RUNNING.md) | Every setup option: models, containers, Postgres, S3, tokens, CLI, TUI, Claude Code |
| [docs/SECURITY.md](docs/SECURITY.md) | What is protected, how, and the known gaps |
| [docs/BUDGETS_OBSERVABILITY.md](docs/BUDGETS_OBSERVABILITY.md) | Token budgets, traces and metrics in detail |
| [docs/BENCHMARKS.md](docs/BENCHMARKS.md) | Noisy benchmarks, hidden grading and the TDD trace |
| [docs/DECISION_MODELS.md](docs/DECISION_MODELS.md) | Jev and Clef wire formats |
| [packages/contracts/CONTRACTS.md](packages/contracts/CONTRACTS.md) | Exact file and API formats |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What MAEF is for and what comes next: experiments, FAIR records, released specialists |
| [docs/USE_CASES.md](docs/USE_CASES.md) | What MAEF is used for, with examples and status |
| [docs/PLAN_OF_ACTION.md](docs/PLAN_OF_ACTION.md) | The roadmap as work packages with checks; [M1_PLAN.md](docs/M1_PLAN.md) for v0.1.0 |
| [docs/HARNESSES.md](docs/HARNESSES.md) | Running Deep Agents and AWS Strands agents inside MAEF (design) |
| [docs/BACKEND_PLAN.md](docs/BACKEND_PLAN.md) | The build plan and what is finished |

## Repository layout

```text
agents/              agent definitions (one folder per agent)
apps/api/            the API, the worker and the `maef` command (Python)
apps/tui/            terminal interface (OpenTUI, Bun)
apps/studio/         web interface (React; read-only for now)
mcp-servers/         tool server definitions
sandbox-profiles/    container settings for sandboxes
model-profiles/      your local model settings (not committed; `maef init` creates one)
examples/            example model profiles and requests
evals/               evaluation suites
schemas/             JSON Schemas for the definition files
docs/                documentation; diagrams in docs/diagrams/design/
tests/               Python tests
```

## Development

```bash
uv sync              # installs the project and the dev group (tests, ruff, ty); add --all-extras for cloud extras
uv run pytest        # same as make test
make check           # ruff, format check and tests
make test            # unit and API tests
make test-oci        # tests that start real containers (needs Podman or Docker)
make test-pg         # ledger tests on Postgres (starts a container)
make test-s3         # artifact tests on an S3-compatible server (starts a container)
uv run maef lint    # check every definition file
cd apps/tui && bun test
```

## Design principles

1. Keep the agent, its model, its tools, the worker and the sandbox as separate, swappable parts.
2. Pin exactly what runs: every run records the agent's Git revision and a digest of its files.
3. Keep tools, secrets and MCP servers away from the assistant that delegates the task.
4. Local first; anything remote talks over a stable, authenticated protocol.
5. Budgets in tokens, reserved before running.
6. Fail closed: missing policy, unpinned images or unavailable isolation stop the run.
7. A human reviews privileged changes; nothing is merged automatically.

## Licence

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). The MAEF name and logo are covered separately in
[TRADEMARKS.md](TRADEMARKS.md).
