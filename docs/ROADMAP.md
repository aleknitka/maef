# Implementation roadmap and engineering slices

## Slice 0 — Repository bootstrap (included)
Python API, YAML registry, read-only Studio, task/budget preview, two agents, tests and docs. No agent execution.

## Slice 1 — Definition authoring and review
- Git-backed CRUD via feature branches/PRs, schema lint, manifest pinning and safe local previews.
- Agent and skills detail views, diffs, policy lint and reviewer approval workflow.
- Endpoint profiles and separate secrets references; never store credentials in definitions.
- Local SQLite run/registry metadata and migration discipline (Alembic).
**Exit:** create, edit, validate, compare, review and publish an agent revision via Studio and Git.

## Slice 2 — Real local execution
- Worker daemon + worker lease protocol, one Linux Docker sandbox profile.
- Minimal LLM harness with a Bedrock Converse adapter first (matches the model endpoint profiles) and tool-call cycle. OpenAI-compatible follows.
- Explicit skill attachment and static tools (MCP following in next slice).
- Run event ledger, token/time/turn enforcement, quota reservations, cancellation and artifact publisher.
- Run detail UI with live events and budget consumption.
**Exit:** validated sample task runs in isolated local environment with reproducible manifest and bounded spend.

## Slice 3 — MCPs and specialist test
- MCP server manifests and per-run startup/shutdown; stdio first, streamable HTTP later.
- Permission scopes, credential broker, tool invocation traces and policy-based tool filtering.
- One pinned stdio MCP (e.g. fetch) attached to `research-analyst`, with tool-trace regression fixtures.
- Host integration via small MCP gateway and CLI.
**Exit:** Codex/Claude Code delegates a bounded research task to `research-analyst`; the result returns with tool traces and brokered credentials, without the parent loading the MCP.

## Slice 4 — Reliability and evaluations
- Durable queue/leases with fencing and idempotent event processing.
- Golden task corpus, output validators, model cost/quality evaluation.
- PostgreSQL migration, OpenTelemetry collector, review metrics and CI smoke tests.
**Exit:** recover from worker crash; compare versioned agents with performance and cost reports.

## Slice 5 — Remote ephemeral workers
- Remote worker auth and outbound-only registration/leases.
- S3-compatible artefact storage, ephemeral VM lifecycle management, resource classes.
- Select local versus remote based on capabilities, residency, policy and cost.
- Stronger isolation options (microVM/gVisor) and audit hardening.
**Exit:** same agent/task spec runs locally and in a disposable remote VM with equivalent result manifest.

## Initial engineering tickets
1. Add JSON Schema export and CI lint for agent.yaml.
2. Replace filesystem-only registry index with Git commit pinned definitions.
3. Implement policy admission and atomic budget reservation schema.
4. Design workers/leases/event persistence and cancellation contract.
5. Build local Docker supervisor prototype with no network/host mounts by default.
6. Build model adapter tests using fake OpenAI-compatible server (usage + tool calls).
7. Add task trace view and SSE endpoint.
8. Implement MCP manifest schema and prompt/tool-review UI.
9. Implement CLI and host MCP gateway.

## Quality gates
CI: format/lint, typecheck, unit tests, schema compatibility, sandbox integration tests before execution releases, dependency security checks and approval for expanded capabilities. Production rollouts require clear trust assumptions and a threat-model review.
