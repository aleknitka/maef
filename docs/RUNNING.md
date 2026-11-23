# Running MAEF

This is the full setup reference. For the five-minute version, see the [README](../README.md#quick-start-no-cloud-account).
For how the parts fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).

- [1. Install](#1-install)
- [2. Choose a model](#2-choose-a-model)
- [3. Access keys (tokens)](#3-access-keys-tokens)
- [4. Start the API and a worker](#4-start-the-api-and-a-worker)
- [5. Give agents work](#5-give-agents-work)
- [6. Containers: sandbox and MCP tools](#6-containers-sandbox-and-mcp-tools)
- [7. Where data is stored](#7-where-data-is-stored)
- [8. Use MAEF from Claude Code or Codex](#8-use-maef-from-claude-code-or-codex)
- [9. Decision models (Jev, Clef)](#9-decision-models-jev-clef)
- [10. Editing agents safely](#10-editing-agents-safely)
- [11. Monitoring and evaluations](#11-monitoring-and-evaluations)
- [12. Settings reference](#12-settings-reference)

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .              # base install: everything local, no cloud libraries
pip install -e '.[dev]'       # + test tools (needed for `make test`)
pip install -e '.[bedrock]'   # + AWS Bedrock models
pip install -e '.[s3]'        # + S3 artifact storage
pip install -e '.[postgres]'  # + Postgres database
maef init                     # local model profile, results folder, SQLite database
```

Works on Linux, WSL2 and macOS. Python 3.11 or newer.

## 2. Choose a model

Agents name a **model profile** (`runtime.model_profile` in `agent.yaml`; the shipped agent uses `local-default`).
Profiles live in `model-profiles/<id>.yaml`. That folder is per machine and never committed, and profiles never contain
secrets, only a pointer to where the secret is (`credential_ref`):

| `credential_ref` | Meaning |
|---|---|
| `none:local` | no key (Ollama, LM Studio) |
| `env:NAME` | read the key from environment variable `NAME` on the **worker** |
| `aws:default-chain` | the normal AWS credential chain (Bedrock only) |

**Local models.** `maef init` writes `local-default` for you. To add more, copy
`examples/model-profiles/ollama.yaml` or `lmstudio.yaml` into `model-profiles/`, change `id` and `model_id`, and point
an agent at the new `id`. Agents with tools need a model that supports tool calling (for example `qwen3`).

- **WSL2:** if Ollama or LM Studio runs on Windows, `localhost` does not reach it. Use the Windows IP
  (`ip route | awk '/default/ {print $3}'`) in `base_url` and set `allow_private_network: true`.
- Plain `http://` is only accepted for localhost or with `allow_private_network: true`; anything else must be `https://`.

**OpenAI** (or any OpenAI-compatible service):

```yaml
schema_version: maef.dev/v1alpha1
id: openai-main
version: 0.1.0
display_name: OpenAI
vendor: OpenAI
api_dialect: openai-compatible
endpoint:
  kind: openai-compatible
  base_url: https://api.openai.com/v1
  model_id: <model-id>
  token_param: max_completion_tokens   # newer OpenAI models need this
credential_ref: env:OPENAI_API_KEY
```

**Anthropic:** the same shape with `api_dialect: anthropic`, `endpoint: {kind: anthropic, model_id: <model-id>}`
and `credential_ref: env:ANTHROPIC_API_KEY`.

**AWS Bedrock:** `api_dialect: bedrock-converse`, `endpoint: {kind: amazon-bedrock, region: <your Region>, model_id: ...}`,
`credential_ref: aws:default-chain`. Needs `pip install -e '.[bedrock]'`.

Optional profile fields: `timeout_seconds` (default 300), `max_retries` (2), `max_output_tokens` (4096, caps each
model reply and therefore the worst-case budget).

## 3. Access keys (tokens)

Every `/api/v1/*` request needs a bearer token. Tokens are created locally, straight in the database, and printed
**once** (only a hash is stored):

```bash
export MAEF_API_TOKEN=$(maef admin token create --name me --scopes admin)   # admin = every scope
maef admin token create --name ci --scopes read,runs:write
maef admin token list
maef admin token revoke <id>
export MAEF_WORKER_CREDENTIAL=$(maef admin worker create --id worker-1)      # one per worker
```

| Scope | Allows |
|---|---|
| `read` | list agents and runs, read events and results, metrics |
| `runs:write` | start and cancel runs |
| `decisions:write` | ask decision models |
| `authoring` | agent drafts |
| `admin` | everything, including the audit log (`GET /api/v1/audit`) |

For quick local experiments `MAEF_AUTH_DISABLED=1` skips token checks, but only for requests from this machine that
send no token, and never grants `admin`.

## 4. Start the API and a worker

```bash
MAEF_EXECUTION_ENABLED=1 make api    # terminal 1, http://127.0.0.1:8000 (docs at /docs)
make worker                          # terminal 2, with MAEF_WORKER_CREDENTIAL exported
```

- Without `MAEF_EXECUTION_ENABLED=1` the API only previews runs (useful for browsing).
- Runs pin the agent's Git revision. With uncommitted changes in `agents/` the API refuses the run unless
  `MAEF_ALLOW_DIRTY=1` (fine for development; the run is marked `dirty`).
- The worker finds the API at `MAEF_API_URL` (default `http://127.0.0.1:8000`; set `MAEF_API_ALLOW_PRIVATE=1` for a
  LAN address). Model keys (`env:NAME`) are read from the **worker's** environment; the API never sends secrets.
- Ctrl+C on the worker finishes the current run first. If a worker disappears, its run fails as `worker_lost` after
  the lease runs out (`MAEF_LEASE_SECONDS`, default 60) and the full reservation is charged.
- Run one worker per machine: at startup it removes leftover MAEF containers.
- `MAEF_ACCOUNT_LIMIT_TOKENS` (default 5,000,000) caps total token use across all runs.

## 5. Give agents work

**Command line** (`maef`; exit codes: 0 ok, 1 run failed or cancelled, 2 usage or API error, 3 timeout):

```bash
maef agents
maef preview research-analyst "Summarise RFC 9110 caching"          # shows the budget, runs nothing
maef run research-analyst "Summarise RFC 9110 caching" --max-tokens 50000 --wait --timeout 300
echo "long instruction" | maef run research-analyst - --json
maef status <run-id>
maef events <run-id> --follow
maef cancel <run-id>
maef result <run-id>                         # the output.md text
maef artifacts <run-id>
maef get sha256:<digest> -o out.bin          # downloads and checks the fingerprint
```

**Terminal UI:** `cd apps/tui && bun install` once, then `make tui`. Keys: `1`/`2`/`3` agents, new run, runs;
`Enter` select; in a new run `Tab` next field, `ctrl+p` preview, `ctrl+s` submit; in a run `c` cancel, `r` result,
`a` artifacts; `Esc` back; `q` quit. Works over SSH and in WSL2, macOS and Linux terminals.

**HTTP:** the API is documented at http://127.0.0.1:8000/docs. Live events are server-sent events:

```bash
curl -N -H "Authorization: Bearer $MAEF_API_TOKEN" http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events
curl -X POST -H "Authorization: Bearer $MAEF_API_TOKEN" http://127.0.0.1:8000/api/v1/runs/$RUN_ID/cancel
```

Reconnect with the header `Last-Event-ID: <last seq>` to continue where you left off. A queued run cancels at once;
a running one stops within one heartbeat (about 15 s) and is charged only for what it used.

**Web Studio** (`cd apps/studio && npm install && npm run dev`, http://127.0.0.1:5173) shows agents and profiles, but
it cannot send tokens yet, so with login on it only works with `MAEF_AUTH_DISABLED=1`.

## 6. Containers: sandbox and MCP tools

MAEF drives containers through the `podman` or `docker` command line only (no Docker socket). It prefers Podman; set
`MAEF_OCI_RUNTIME=podman|docker` to choose.

- **Linux:** `sudo apt install podman` (or `dnf`).
- **WSL2:** install Podman inside the distro like Linux. Docker Desktop also works (`MAEF_OCI_RUNTIME=docker`), but it
  is not rootless, so use a sandbox profile with `require_rootless: false`.
- **macOS:** `brew install podman && podman machine init && podman machine start`.

**Sandbox.** Agents that have the `run_command` tool get one container per run, from a profile in `sandbox-profiles/`:
read-only, all Linux capabilities dropped, no network, no host folders, memory/CPU/process limits, a small temporary
`/workspace`, and an unprivileged user. Images must be pinned by digest (`image@sha256:...`); for experiments
`MAEF_ALLOW_UNPINNED_IMAGES=1` allows tags. A profile with `require_rootless: true` refuses to run on a rootful runtime.

**MCP tool servers** are defined in `mcp-servers/<id>.yaml` and attached to an agent with
`mcp_servers: ["<id>@<version>"]`. Each runs in its own container; its tools appear to the model as
`mcp__<id>__<tool>`. Secrets are passed as environment variable *names* (`secrets: {API_KEY: env:VAR}`), never stored
in the file. Network modes:

| `network` | Meaning |
|---|---|
| `none` | no network at all |
| `allowlist` | a private per-run network whose only exit is a filtering proxy: listed hosts and ports only, public addresses only, never cloud metadata or your LAN |
| `open` | unfiltered (discouraged) |

`allowlist` and `open` also need `permissions: {network_open: true}` on the agent. The shipped `research-analyst` uses
the `fetch` server in `allowlist` mode with `allowed_hosts: ["*"]` (any public site).

## 7. Where data is stored

By default everything stays on this machine.

| Data | Default | Change with |
|---|---|---|
| Run log, tokens, keys (hashed), audit | SQLite file `runs/maef.db` | `MAEF_DB=/path/file.db` or `MAEF_DB=postgresql://...` |
| Result files (artifacts) | folder `artifacts/` | `MAEF_ARTIFACTS_DIR=/path` or `MAEF_ARTIFACTS_URL=s3://bucket/prefix` |
| Agent definitions | Git, `agents/` | — |
| Model profiles | `model-profiles/` (not committed) | — |

Artifacts are stored under their sha256 fingerprint, written once, and checked on every read. Downloads are always
served as attachments, never rendered in the browser.

**Postgres (optional).** `pip install -e '.[postgres]'` and point the API, workers and `maef admin` at the same
database with `MAEF_DB=postgresql://user:pass@host/db`; tables are created on first start. A throwaway local server:

```bash
podman run -d --rm -p 127.0.0.1:5432:5432 -e POSTGRES_PASSWORD=maef docker.io/library/postgres:17-alpine
export MAEF_DB=postgresql://postgres:maef@127.0.0.1/postgres
```

**S3-compatible storage (optional).** `pip install -e '.[s3]'` and set `MAEF_ARTIFACTS_URL=s3://bucket/prefix` on the
API and every worker. Optional: `MAEF_S3_ENDPOINT_URL` (non-AWS servers), `MAEF_S3_REGION`,
`MAEF_S3_ADDRESSING_STYLE=path` (most non-AWS servers), `MAEF_S3_SSE=0` (servers without encryption support),
`MAEF_S3_ALLOW_PRIVATE=1` (server on your LAN), `MAEF_S3_ACCESS_KEY_REF=env:NAME` and `MAEF_S3_SECRET_KEY_REF=env:NAME`
(otherwise the AWS credential chain).

A local S3 server for testing (SeaweedFS, pinned image):

```bash
podman run -d --rm -p 127.0.0.1:8333:8333 -e AWS_ACCESS_KEY_ID=maef -e AWS_SECRET_ACCESS_KEY=maefsecret \
  docker.io/chrislusf/seaweedfs@sha256:4b42c212543a0acd4c0e675215ecdcaf62a186ef98def76c7e12701871222d15 \
  server -s3 -s3.port=8333 -volume.max=20 -master.volumeSizeLimitMB=64
export AWS_ACCESS_KEY_ID=maef AWS_SECRET_ACCESS_KEY=maefsecret     # create the bucket once with any S3 client
MAEF_ARTIFACTS_URL=s3://maef-artifacts/artifacts MAEF_S3_ENDPOINT_URL=http://127.0.0.1:8333 \
  MAEF_S3_ADDRESSING_STYLE=path MAEF_S3_SSE=0 MAEF_S3_REGION=us-east-1 make api
```

On AWS: create the bucket in your project's selected Region (AWS Settings > View all projects > Overview > Additional
Info > Region), keep Block Public Access and default encryption on, and give the API and workers only this access:

```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::<bucket>/<prefix>*"},
  {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>",
   "Condition": {"StringLike": {"s3:prefix": "<prefix>*"}}}]}
```

`s3:ListBucket` is required: without it S3 answers "forbidden" instead of "not found" for a missing file, and the
first upload of every new file fails.

## 8. Use MAEF from Claude Code or Codex

A coding assistant can hand tasks to MAEF agents through a small MCP gateway. The assistant only sees
`list_agents`, `delegate`, `run_status`, `run_result`, `cancel_run` and `decide`; it never loads the agents' tools,
MCP servers, secrets or instructions.

```bash
claude mcp add maef --env MAEF_API_URL=http://127.0.0.1:8000 --env MAEF_API_TOKEN=$MAEF_API_TOKEN -- maef mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.maef]
command = "maef"
args = ["mcp"]
env = { MAEF_API_URL = "http://127.0.0.1:8000", MAEF_API_TOKEN = "<token>" }
```

Agent output is treated as untrusted: the gateway wraps it in `<maef-result run_id=... status=...>` and caps it at
100 KiB.

## 9. Decision models (Jev, Clef)

Decision models answer typed questions (`choice`, `score`, `noul`) about a state instead of writing text. Copy
`examples/model-profiles/jev-decisions-api.yaml` (key in `DECISIONS_API_KEY`) or `clef-omni-workers-ai.yaml` (key in
`CLOUDFLARE_API_TOKEN`, plus your account id) into `model-profiles/`, then:

```bash
maef decide jev-decisions-api --state @ticket.json --wait \
  --question "team:choice:Which team should handle this?:billing=payments and refunds,technical=bugs and outages" \
  --question "urgent:noul:Is this urgent?" \
  --question "anger:score:How upset is the customer?:calm,annoyed,angry"
```

These calls leave your machine (the providers are web services). Answers are checked against the questions and stored
as `answers.json`. Details: [DECISION_MODELS.md](DECISION_MODELS.md).

## 10. Editing agents safely

Agent definitions live in Git and are never edited on `main` by MAEF. Drafts are off by default
(`MAEF_AUTHORING_ENABLED=1`) and need the `authoring` scope.

```bash
maef lint                                          # check all definition files
maef draft new research-analyst                    # own branch + working folder
maef draft write <id> agents/research-analyst/SYSTEM.md ./SYSTEM.md
maef draft review <id>                             # prompt diff, permission diff, lint
maef draft publish <id> -m "Tighten citation rules"   # commits on the draft branch, prints the sha
maef run research-analyst "..." --revision <sha> --wait   # try exactly that version
git merge maef/draft/research-analyst/<id>         # a human merges after review
maef draft discard <id>
```

Only text files under `agents/<agent>/` can be written (max 256 KiB), and publishing is refused while lint reports
errors. `maef schema export --check` fails if `schemas/*.schema.json` are out of date.

## 11. Monitoring and evaluations

- **Traces:** `maef otel export --endpoint http://127.0.0.1:4318` sends finished runs to any OpenTelemetry (OTLP/HTTP)
  collector: one span per model turn, tool call, sandbox and MCP server, with no prompt or tool content. Set
  `MAEF_OTLP_ENDPOINT` on the API to export every run automatically.
- **Metrics:** `GET /api/v1/metrics` (scope `read`) in Prometheus format: runs, tokens, reservations, durations,
  budget exhaustions, lost workers, tool calls, artifact bytes.
- **Evaluations:** golden tasks in `evals/<suite>.yaml`.
  `maef eval run smoke --revision <sha-a> --revision <sha-b>` runs the suite on each agent version and prints pass
  rate and tokens.

More: [BUDGETS_OBSERVABILITY.md](BUDGETS_OBSERVABILITY.md).

## 12. Settings reference

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `MAEF_DB` | API, `maef admin`, `maef init` | `runs/maef.db` | SQLite path or `postgresql://` URL |
| `MAEF_ARTIFACTS_DIR` | API, worker | `artifacts/` | local result folder |
| `MAEF_ARTIFACTS_URL` | API, worker | unset | `s3://bucket/prefix` to use S3 instead |
| `MAEF_S3_*` | API, worker | — | S3 options (section 7) |
| `MAEF_EXECUTION_ENABLED` | API | off | allow real runs (otherwise preview only) |
| `MAEF_ALLOW_DIRTY` | API | off | allow runs with uncommitted agent changes |
| `MAEF_ACCOUNT_LIMIT_TOKENS` | API | 5,000,000 | total token cap |
| `MAEF_LEASE_SECONDS` | API | 60 | how long a silent worker keeps its run |
| `MAEF_AUTH_DISABLED` | API | off | skip token checks for local requests |
| `MAEF_AUTHORING_ENABLED` | API | off | allow agent drafts |
| `MAEF_AUTHORING_NAME`, `MAEF_AUTHORING_EMAIL` | API | MAEF Authoring | author of draft commits |
| `MAEF_OTLP_ENDPOINT`, `MAEF_OTLP_HEADERS_REF`, `MAEF_OTLP_ALLOW_PRIVATE` | API | unset | automatic trace export |
| `MAEF_API_URL` | worker, CLI, TUI | `http://127.0.0.1:8000` | where the API is |
| `MAEF_API_ALLOW_PRIVATE` | worker | off | allow an API on a LAN address |
| `MAEF_API_TOKEN` | CLI, TUI, gateway | — | your access token |
| `MAEF_WORKER_CREDENTIAL` | worker | — | the worker's credential |
| `MAEF_WORKER_ID` | worker | hostname-pid | label in logs |
| `MAEF_OCI_RUNTIME` | worker | auto (Podman first) | `podman` or `docker` |
| `MAEF_ALLOW_UNPINNED_IMAGES` | worker | off | allow image tags instead of digests |
