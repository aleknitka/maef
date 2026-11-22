# Architecture — MAEF

## Executive decision
Implement a modular monolith control plane in Python (FastAPI/Pydantic), TypeScript React Studio, and an independently deployed worker process. Treat worker messaging as a protocol rather than an in-process Python call. Begin with a single local worker; allow future ephemeral remote workers without changing agent definitions. Use SQLite for bootstrap and PostgreSQL before enabling concurrent remote execution. The `Ledger` has two adapters behind one interface (SQLite, and PostgreSQL via the optional `postgres` extra; `MAEF_DB` is a path or a `postgresql://` URL); on Postgres the queue is durable and multi-claimer safe (`FOR UPDATE SKIP LOCKED`).

## Boundaries

### Control plane
Authoritative registry index, task API, admission control, budgets, policy decisions, worker matchmaking, execution state, audit trail, artefact metadata. The control plane MUST NOT directly execute untrusted generated code.

### Execution plane
Workers authenticate to the control plane, claim eligible leases, verify pinned revisions and policy digests, provision a sandbox, run a harness, stream events and artefacts, then tear down. Worker host receives minimal credentials through scoped ephemeral delivery.

### Harness
Inside the sandbox, combines pinned agent instructions, the caller's task, scoped context and permitted skills; establishes the model session; exposes allowlisted tools/MCP; records every tool and model invocation; applies turn/time/usage controls; exits with an explicit terminal reason. Model adapters and CLI adapters have distinct accounting guarantees.

### Model endpoint profiles
A profile specifies API dialect (OpenAI-compatible, Anthropic-native, CLI adapter), endpoint URI, model identifier, capability flags, timeout/retry policy, price schedule ID and a reference to a credential secret. The definition never embeds an API key. Allow opt-in connection tests and strict endpoint/network allowlisting.

### Tool and MCP manifests
Each tool or MCP server is an independently versioned declaration with transport, executable/image reference, required secret references, allowed filesystem/network access and permission category. Agent definitions pin *specific* tool versions. Runtime only instantiates the granted subset. HTTP MCP uses contemporary authorisation guidance; stdio tools receive scoped environment credentials, not inherited full host environments.

### Registry
Git is source of truth for desired agent definitions, with immutable commit refs and tags. Registry indexes validated definitions into DB for search and query. Runtime resolves agent by `(repo, revision, definition path, content digest)`, not by mutable branch name. Drafts and pull requests represent proposed changes; publishing requires checks.

### Workers and sandbox isolation
Phase 1: Linux rootless Docker in restricted profile; no host Docker socket inside the sandbox; capability drops; read-only base filesystem; memory/CPU/PID quotas; scoped writable workspace; network egress deny-by-default; temporary credentials. Rootless containers mitigate risk but are NOT a hard security boundary for hostile code. For high-trust separation, evaluate gVisor or microVMs. Docker socket must be held by a privileged supervisor separate from web-facing processes.

### Artifacts
Outputs go to a local filesystem store first, addressed by hash and referenced in a run manifest. Later adopt S3-compatible object storage. Source code changes stay in an isolated Git worktree/branch and are NEVER automatically merged. Video files and logs are not checked into Git by default.

## Proposed task lifecycle
`created -> validated -> admitted -> queued -> leased -> provisioning -> running -> validating -> completed | failed | cancelled | timed_out | budget_exceeded`.

Failures to provision, expired leases and worker loss must be classified separately. Retrying is safe only if steps are idempotent or explicitly marked re-executable; external side-effect tools require caution.

## Proposed remote-worker protocol
1. Worker registers identity, runtime/image capabilities and current capacity.
2. Worker authenticates using a short-lived machine credential; control plane records host fingerprint.
3. Worker polls for lease or receives dispatch after matching capabilities and policy.
4. Lease includes immutable task ID, definition revision/digest, sandbox profile, budget ceiling and expiring authorisations — never master credentials.
5. Worker periodically renews lease and transmits sequenced events and usage.
6. Controller reconciles out-of-order/duplicate events using monotonic sequence numbers and idempotency keys.
7. Results uploaded via short-lived grants with digest validation.
8. Worker is drained/deregistered; ephemeral machine may be terminated.

Remote nodes behind NAT should use outbound worker-initiated channels. Cloud workers should be ephemeral and disposable, with no persistent secrets or inbound ports by default.

## Contracts
See `packages/contracts/CONTRACTS.md`. Use OpenAPI from FastAPI for user-facing control endpoints; define stable JSON Schema for tasks, agent definitions, worker leases, result manifests and events. Version schemas deliberately.

## Deployment path
- Bootstrap: FastAPI + Vite dev server, YAML filesystem registry, no execution.
- Local MVP: database, policy engine, durable task queue/lease, rootless Docker worker, one model adapter and tracked budgets.
- Homelab: containerised services, reverse proxy/authentication, PostgreSQL, local artifact volume, optional OpenTelemetry collector.
- Remote: worker credentials, outbound HTTPS, object storage, VM provisioning and lifecycle automation.

## Key risks
- API cost guardrails cannot guarantee strict USD ceilings when provider pricing/usage is delayed or tools bill out of band. Reserve estimated upper bounds and fail closed on unknown pricing.
- Arbitrary MCP packages and downloaded skills are supply-chain execution risks.
- User-authored task prompts and retrieved documents may carry prompt injection. Treat them as untrusted.
- Remote worker failure and duplicate dispatch require reconciliation, fencing tokens and idempotent persistence.
- Git versions alone do not make runtime execution reproducible; pin model metadata, dependency and image digests, tool configuration and context hashes.
