# Backend implementation plan

Approved 2026-11-22. Each step is self-contained: tests pass and it is committed on its own. Vocabulary follows the `codebase-design` skill (module, interface, seam, adapter). Rule: no port without two adapters (production + test).

| # | Step | Interface (seam) | Adapters | Done when |
|---|------|------------------|----------|-----------|
| 0 | Deepen existing code | `Definitions.resolve(agent_id) -> PinnedDefinition`; `Ledger` (only SQL owner); `Runs` (`submit`/`preview`/`get`/`list`/`recover`) | SQLite (tmp_path stand-in) | `main.py` is routing only; pinned instruction bytes are exactly what the model receives |
| 1 | Credentials + egress | `Credentials.resolve(ref) -> Secret`; `EgressPolicy.check(url)` | env, AWS chain, fake | no adapter reads `os.environ`; SSRF guard (private/metadata ranges) shared |
| 2 | Tool loop + budget guard + local models | `BudgetGuard`; `ToolSet` (`specs`, `invoke`); tool calls in `ModelAdapter` | built-in read-only tools, fake; Bedrock, Anthropic, OpenAI-compatible incl. **Ollama and LM Studio** | multi-turn tool runs stop correctly on every limit, verified against local-model request shapes |
| 3 | Worker process + leases | `WorkerChannel` (`claim`/`heartbeat`/`report`/`complete`), fencing tokens | in-process, local HTTP | execution leaves the API process; killed worker → `worker_lost`; duplicate claim rejected |
| 4 | Sandbox (OCI) | `Sandbox.provision(profile) -> Session` | **Podman (preferred)**, Docker (compatible), fake | Linux, WSL2 and macOS (podman machine); read-only root, dropped caps, quotas, egress denied, no runtime socket inside |
| 5 | MCP manifests | `ToolSet` adapter for stdio MCP in sandbox | stdio MCP | `research-analyst` runs with a pinned fetch MCP and tool traces |
| 6 | Artifacts | `ArtifactStore.put/open` | local content-addressed, in-memory | manifest artifacts digest-verified |
| 7 | Live events + cancel | SSE from `Ledger`; `POST /runs/{id}/cancel` | — | Studio can follow and cancel a run |
| 8 | Host gateway | `maef` CLI + small MCP server over `Runs` | — | Claude Code/Codex delegate without loading agent tools |
| 9 | Decision models (Jev, Clef) | `DecisionAdapter.decide(state, questions)` | Jev, Clef, fake | decisions metered in the same ledger |
| 10 | Definition authoring | JSON Schema export, Git-backed edit/publish | — | publish pins a commit with prompt/permission diffs |
| 11 | Auth + audit | auth on all routes, audit events, worker machine credentials | — | safe to bind beyond 127.0.0.1 |
| 12 | Postgres + durable queue | second `Ledger` adapter | SQLite, Postgres | same suite passes on both |
| 13 | Remote ephemeral workers | outbound HTTPS `WorkerChannel` | — | same task, equivalent manifest locally and on a disposable VM |
| 14 | Observability + evals | OTel export from ledger events; golden tasks | — | agent versions compared on quality and tokens |

- Step 11 also includes: enforced egress allowlist proxy for open-network MCP servers (step 5 only gates `network: open` behind `permissions.network_open`; an open server can reach any host).

Ordering: 0→4 sequential; 5–9 depend on 4; 11 must land before any network exposure.

## Status
- [x] 0 · [x] 1 · [x] 2 · [x] 3 · [x] 4 · [x] 5 · [ ] 6 · [ ] 7 · [ ] 8 · [ ] 9 · [ ] 10 · [ ] 11 · [ ] 12 · [ ] 13 · [ ] 14
