<p align="center">
  <img src="assets/maef-logo.png" alt="MAEF — Modular Agent Execution Framework" width="860">
</p>

# MAEF — Modular Agent Execution Framework

**Status: architecture + runnable registry/studio bootstrap (v0.1.0).** Not an operational agent executor yet.

MAEF is a local-first, Git-native framework for defining specialist agents, reviewing their skills and configuration, and eventually dispatching isolated, budget-controlled tasks to local or remote workers. Main clients (Codex, Claude Code, etc.) should only need the small MAEF gateway, not every specialist MCP.

## Included now
- Typed YAML agent definitions and a local Git-friendly registry.
- Read-only FastAPI endpoints to inspect agent definitions and context files.
- Task **preview** endpoint showing effective bounded budgets and selected runtime profile.
- React Studio for inspecting agents, skills, model/sandbox profiles, outputs and task previews.
- One example agent definition and API tests.
- Architecture, contracts, ADRs, security considerations and phased roadmap.

**Opt-in execution:** with `MAEF_EXECUTION_ENABLED=1`, `POST /api/v1/runs` admits and queues a budget-reserved run (needs a gitignored model profile and a clean git tree or `MAEF_ALLOW_DIRTY=1`); a separate worker process claims it and calls the model (see below). It is off by default and returns 501. Runs and append-only events live in a SQLite ledger (`MAEF_DB`, default `runs/maef.db`); `MAEF_ACCOUNT_LIMIT_TOKENS` (default 5,000,000) caps total token use.

**Not implemented:** MCP over HTTP, MCP gateway server, remote worker machine credentials and sandboxes (the worker is a plain process using one shared token), actual spend enforcement, authentication, publishing, Git commit automation. Studio clearly labels previews as non-executions.

## Requirements
- Python >=3.11
- Node.js >=20 and npm

## Start the API

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
make api
```

API: http://127.0.0.1:8000/docs

## Start a worker (second terminal)

The API only admits and queues runs; a worker process executes them. Both need the same `MAEF_WORKER_TOKEN` (without it every `/api/v1/worker/` route returns 503):

```bash
export MAEF_WORKER_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
MAEF_EXECUTION_ENABLED=1 make api      # terminal 1
make worker                            # terminal 2
```

Worker settings: `MAEF_API_URL` (default `http://127.0.0.1:8000`; set `MAEF_API_ALLOW_PRIVATE=1` for a LAN address), `MAEF_WORKER_ID` (default hostname-pid). Model credentials (`env:NAME` references) are read from the worker's own environment, never sent by the API. SIGINT/SIGTERM finish the run in flight, then exit. The API fails runs whose worker stops heartbeating (lease `MAEF_LEASE_SECONDS`, default 60) as `worker_lost`, charging their full reservation.

## Follow and cancel a run

```bash
curl -N http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events     # live SSE; resume with -H 'Last-Event-ID: 12'
curl -X POST http://127.0.0.1:8000/api/v1/runs/$RUN_ID/cancel # 202 {"outcome": "cancelled" | "requested"}
```

A queued run is cancelled immediately; a running one stops within one worker heartbeat (15 s) or sooner, and is charged only for the usage so far.

## Terminal UI

A keyboard-first terminal front end (OpenTUI + React on Bun) over the HTTP API and SSE only. It works over SSH and in WSL2, macOS and Linux terminals.

```bash
cd apps/tui && bun install     # once
# start the API and a worker as above, then from the repo root:
make tui                       # MAEF_API_URL defaults to http://127.0.0.1:8000
```

Keys: `1`/`2`/`3` agents, new run, runs; `Enter` select; new run: `Tab` next field, `ctrl+p` preview, `ctrl+s` submit, `Esc` back; run detail: `c` cancel, `r` result, `a` artifacts, `Esc` back; `ctrl+r` retry after an API error; `q`/`ctrl+c` quit. Tests: `cd apps/tui && bun test`.

## Start the Studio (second terminal)

```bash
cd apps/studio
npm install
npm run dev
```

Studio: http://127.0.0.1:5173

## Local models (Ollama, LM Studio)

Copy an example profile into `model-profiles/` (profiles are local, per-machine files):

```bash
cp examples/model-profiles/ollama.yaml model-profiles/     # http://localhost:11434/v1
cp examples/model-profiles/lmstudio.yaml model-profiles/   # http://localhost:1234/v1
```

Edit `model_id` to a model you have pulled, then set an agent's `runtime.model_profile` to the profile `id`. Both use `credential_ref: none:local`, so no key is needed.

- **WSL2:** if the server runs on the Windows host, `localhost` will not reach it. Use the host IP from `ip route | awk '/default/ {print $3}'` as `base_url` host and set `allow_private_network: true` in the profile. On macOS and Linux use `localhost`.
- **Tools:** an agent with `tools:` needs a model that supports tool calling (for example `qwen3`); models without it will ignore or fail the tool definitions. Tool-less agents work with any chat model.

## Sandbox runtimes

Agents that list `run_command` execute inside an OCI container (one per run: read-only root, all capabilities dropped, no network, no mounts, memory/CPU/pids limits, tmpfs `/workspace`). The worker uses only the CLI surface shared by `podman` and `docker` (no runtime API socket) and prefers Podman. Set `MAEF_OCI_RUNTIME=podman|docker` to force one. Profiles live in `sandbox-profiles/`; images need POSIX `sh`, `cat`, `mkdir` and `sleep infinity`.

- **Linux:** `sudo apt install podman` or `sudo dnf install podman` (rootless by default).
- **WSL2:** install Podman inside the distro, same as Linux. Docker Desktop's WSL integration also works with `MAEF_OCI_RUNTIME=docker`, but Docker Desktop is rootful inside its VM, so use a profile with `require_rootless: false`.
- **macOS:** `brew install podman && podman machine init && podman machine start` (rootless by default).

A profile with `require_rootless: true` fails closed (`sandbox_unavailable`) on a rootful runtime. Images must be digest-pinned (`image@sha256:...`); `restricted-local` uses the tag `busybox:1.37`, so in development start the worker with `MAEF_ALLOW_UNPINNED_IMAGES=1` until it is pinned (recorded in the `sandbox.provisioned` event). The worker removes leftover `maef.run`-labelled containers at startup, so run one worker per host. `make test-oci` runs the real-runtime tests; they skip when no runtime is installed.

## MCP servers

Stdio MCP servers are declared in `mcp-servers/<id>.yaml` (image, command, non-secret `env`, `secrets` as `ENV_NAME: env:VAR`, `network`, `permission`) and attached with `mcp_servers: ["<id>@<version>"]` in `agent.yaml`. Each server runs in its own container next to the run and its tools appear to the model as `mcp__<id>__<tool>`; tool calls are traced (sizes only). Servers need `sleep infinity` in the image, like sandbox images, and a container runtime on the worker host. A server with `network: open` only runs if the agent sets `permissions: {network_open: true}`; there is no egress allowlist yet, so such a server can reach any host. `research-analyst` uses the reference `fetch` server; its image is tag-only, so start the worker with `MAEF_ALLOW_UNPINNED_IMAGES=1` until you pin it by digest.

## Delegating from Claude Code / Codex

A host coding agent hands bounded tasks to MAEF agents through the `maef` CLI or a small stdio MCP gateway. The host
never loads the agents' tools, MCP servers, credentials or instructions; it only sees `list_agents`, `delegate`,
`run_status`, `run_result` and `cancel_run`.

```bash
pip install -e .                       # installs the `maef` command
# start the API and a worker as above, then:
claude mcp add maef --env MAEF_API_URL=http://127.0.0.1:8000 -- maef mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.maef]
command = "maef"
args = ["mcp"]
env = { MAEF_API_URL = "http://127.0.0.1:8000" }
```

CLI (`MAEF_API_URL` defaults to `http://127.0.0.1:8000`; exit codes 0 ok, 1 run failed/cancelled, 2 usage/API error, 3 timeout):

```bash
maef agents
maef preview research-analyst "Summarise RFC 9110 caching"
maef run research-analyst "Summarise RFC 9110 caching" --max-tokens 50000 --wait --timeout 300   # events on stderr, result on stdout
echo "long instruction" | maef run research-analyst - --json
maef status $RUN_ID && maef events $RUN_ID --follow && maef cancel $RUN_ID
maef artifacts $RUN_ID && maef get sha256:... -o out.bin [--force]
maef result $RUN_ID
```

Agent output is untrusted: the gateway wraps it in `<maef-result run_id=... status=...>` and caps it at 100 KiB.

## Decision models (Jev, Clef)

Decision models answer typed questions (`choice`, `score`, `noul`) about a state instead of writing text. Copy an example from
`examples/model-profiles/` (`jev-decisions-api.yaml` with `DECISIONS_API_KEY`, `clef-omni-workers-ai.yaml` with `CLOUDFLARE_API_TOKEN` and your account id) into `model-profiles/`, then:

```bash
maef decide jev-decisions-api --state @ticket.json --wait \
  --question "team:choice:Which team should handle this?:billing=payments and refunds,technical=bugs and outages" \
  --question "urgent:noul:Is this urgent?" \
  --question "anger:score:How upset is the customer?:calm,annoyed,angry"
```

Runs go through the same queue, worker and token ledger (`POST /api/v1/decisions`, the `decide` MCP gateway tool); answers are the `answers.json` artifact and are untrusted content. Wire formats, limits and known gaps: `docs/DECISION_MODELS.md`.

## Validate

```bash
pytest -q
cd apps/studio && npm run build
```

## Monorepo layout

```text
apps/api/              FastAPI registry and task preview control plane
apps/studio/           React TypeScript management UI
agents/                Git-managed reusable specialist definitions
packages/contracts/    Versioned contracts and schema documentation
docs/                  Product brief, architecture, ADRs, roadmap, security
infra/                 Future worker and deployment assets
examples/              Sample delegation payloads
tests/                 API contract/behaviour tests
```

## Design principles
1. Distinguish agent definition, harness, sandbox, worker and model endpoint.
2. Use versioned contracts and immutable definition revisions per run.
3. Keep execution tools, secrets and MCPs away from the parent coding agent.
4. Local-first; remote workers speak a stable authenticated protocol.
5. Explicit budgets; all cost calculations in precise units, with admission reservations.
6. Fail closed on missing policy, unknown cost model or unavailable isolation.
7. Human review for privileged actions and promotion of produced changes.

See [Architecture](docs/ARCHITECTURE.md) and [Roadmap](docs/ROADMAP.md).
