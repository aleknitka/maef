# Threat model and security requirements

## Trust boundaries
(1) Browser -> control-plane API; (2) control plane -> worker; (3) worker -> sandbox; (4) sandbox -> model endpoint/tool/MCP; (5) worker -> artifact store; (6) untrusted Git/skill/tool source -> runtime.

## Status of the controls
Each line says what the code does today and where. "Not implemented" means the requirement stands but nothing enforces it yet.

| Control | Status | Where |
|---|---|---|
| Authentication, authorisation, audit for network-accessible deployments | **Partly met.** Scoped bearer tokens (hashed, constant-time compare) on every `/api/v1/*` route; per-worker credentials bound to a lease; append-only `audit_log`. Gaps below: no token expiry, no rate limit, https not enforced off loopback. | `auth.py`, `main.py` (`authorize`, `_audit`), `ledger.py` |
| Immutable Git definition | **Met for the pin.** A run records the commit and a digest of the definition files; dirty or unknown git state is refused unless `allow_dirty` is set and recorded. | `ledger.py` (`pinned.dirty`), `definitions.py` |
| Signed or approved registries | **Not implemented.** Sandbox images must be pinned by digest and MCP manifests are compared by value, but nothing verifies a signature. | `models.py` (`is_pinned_image`), `sandbox.py` |
| Capabilities granted explicitly per agent | **Met for network and tools.** Network is `none` unless the agent permits it and the MCP manifest asks for it; a worker runs only the tools the lease names. | `models.py` (`AgentPermissions`), `sandbox.py`, `worker.py` |
| No docker socket in generated-code containers | **Met.** The sandbox shells out to the runtime CLI and mounts nothing into the container. | `sandbox.py` (module docstring) |
| Resource limits and bounded execution | **Met.** Memory, pids, tmpfs size, read-only root, all capabilities dropped, per-command timeout, per-stream output byte cap (the command is stopped on overflow, result marked `truncated`), run budget of tokens, turns and seconds. No CPU-seconds or storage ceiling. | `sandbox.py` (`run_capped`, `_OciSession`), `tools.py`, `guard.py` |
| Rootless supervisor, policy-aware sandbox | **Met by refusal.** A rootful runtime is refused when the profile requires rootless; an unpinned image is refused unless an env override is set. | `sandbox.py` (`require_rootless`, `unpinned_image`) |
| Egress restrictions verified by integration tests | **Met on Linux.** `tests/test_sandbox_oci.py` covers network `none` and the allowlist proxy; it runs in CI on Ubuntu with rootless podman (`.github/workflows/containers.yml`) and locally with `make test-oci`. Not run in CI on macOS (no podman machine on GitHub's runners). | `tests/test_sandbox_oci.py`, `tests/test_egress_proxy.py` |
| Secrets through a broker, redacted from logs, traces and artefacts | **Partly met; no redaction.** Secrets are resolved only by the worker, from its own environment, are never in a lease, and events, spans and log lines carry sizes and ids, not values. Nothing scans text for secrets: with `MAEF_RECORD_TRANSCRIPT=1` the transcript artefact holds raw tool output, and an agent can write any value it can read into an artefact. | `worker.py` (module docstring, `_start_mcp`), `credentials.py`, `otel.py` |
| Workspace attachment policy | **Met by absence.** No host path is ever mounted. Draft writes reject `..`, absolute paths and symlinks. | `sandbox.py`, `drafts.py` |
| Explicit approval for destructive actions, side effects, publishing | **Not implemented.** There is no approval flow. A draft shows a permission diff and a human merges the branch in Git; nothing else asks. | `drafts.py` (permission diff) |
| Deny launch if budget and pricing policy cannot be evaluated | **Not implemented as written.** Budgets are integer tokens; there is no price table, so nothing is "unpriced". Admission fails closed when the token reservation does not fit. | `ledger.py` (admission), `runs.py` |
| Endpoint allowlist and SSRF protection | **Partly met; see the DNS-rebinding gap.** Model endpoints, the worker channel, the client and the OTLP exporter resolve the name, deny if any address is loopback (unless allowed), private (unless allowed) or link-local/metadata, and never follow redirects. MCP servers on `network: allowlist` go through a proxy that resolves the name itself and connects to the vetted IP. `network: open` is unfiltered. | `egress.py`, `adapters/http.py`, `egress_proxy.py` |

### Known gap: DNS rebinding on direct HTTP calls
`EgressPolicy.check` resolves the host and vets every address, then `urllib` resolves it again when it connects. A DNS server that answers a public address first and a private or metadata address second passes the check and reaches the private address. Affected callers are every user of `EgressPolicy.check` that then opens a connection with `urllib`: the model adapters (`adapters/http.py`), the worker's channel to the API (`channel.py`), `MaefClient` (`client.py`) and the OTLP exporter (`otel.py`). The S3 artefact store vets its endpoint once at construction and boto3 resolves on its own. Not affected: MCP servers behind the allowlist proxy, which connects only to the IP it vetted. The names concerned are operator-configured (model profiles, `MAEF_OTLP_ENDPOINT`, the API URL), so exploiting it needs a hostname the operator chose to be attacker-controlled. Until the callers connect to the vetted IP, use IP literals or hostnames you trust.

### Known gaps for unattended or shared deployments (R7, R8)
Open: bearer tokens have no expiry; no rate limiting on any route; revoking a token does not close an event stream that token already opened; rejected requests each add an audit row, so a flood grows the table without bound; https is not required away from loopback (TLS termination is the operator's job); strict `Bearer` parsing is not enforced. Do not expose the API beyond localhost until these are closed (plan package 6.4).

### Readiness status
Fixed in phase 1: R1 token accounting (reported usage is clamped to the reservation with a `budget.overreport` event; lease expiry charges recorded usage or the whole reservation; `ledger.py` `_settle`, `expire_leases`). R2 worker resilience (sends retry with backoff to the lease deadline; runs end with a classified reason, `endings.py`). R3 gateway escaping (fresh random delimiter per result, closing tags neutralised; `gateway.py`). R4 sandbox output cap (`sandbox.py` `run_capped`). R7 credentials and fetch (worker resolves `env:` names only when a local profile, MCP manifest or `MAEF_WORKER_ALLOWED_CREDENTIALS` names them; profile credentials are bound to their endpoint; MCP secrets are bound to the worker's local manifest; the shipped `fetch` server lists hosts; `credentials.py`, `worker.py`, `mcp-servers/fetch.yaml`). R8 above is open.

## Additional concerns
MCP server descriptions are not trusted instructions. An MCP tool may be compromised or return malicious text. Image digests are pinned and manifests are compared by value (see below); signatures and approvals are not implemented. MCP servers do not inherit parent MCP configuration. Even rootless containers share a host kernel. For hostile third-party code, evaluate gVisor/Firecracker microVMs or remote hosts with stronger separation. Limit access to local files and cloud metadata credentials.

## Desktop constraints
On macOS and Windows Docker usually executes inside a VM; Linux execution semantics differ. The sandbox refuses a rootful runtime and unpinned images instead of downgrading; it does not yet record worker OS or architecture per run.

## Review process
A definition change is reviewed as code. A draft computes a permission diff (tools, MCP servers, new secrets, open network, budget increases, runtime changes; `drafts.py` `PermissionDiff`) and a human merges the branch. Drafts can write only under `agents/<id>/`, so they cannot edit MCP manifests or model profiles; those change through normal Git review, and nothing forces a re-review when they do.

MCP manifest binding. Before resolving any secret for an MCP server, the worker requires its own project's `mcp-servers/<id>.yaml` to equal the manifest in the lease (every field: image and digest, command, env, secrets mapping, network mode, hosts and ports, user, and so on). On a mismatch or a missing local file the server is not started and the run ends `mcp_manifest_mismatch:<id>` (infrastructure); the log names the differing fields, never values. Servers with no `secrets` are exempt, so a worker with no project folder can still run them; they hold nothing to steal and stay inside their sandbox and network mode.

Worker credentials. The worker resolves an `env:NAME` reference only when NAME appears in the project's own `model-profiles/` or `mcp-servers/` files or in `MAEF_WORKER_ALLOWED_CREDENTIALS`; a lease cannot make it read any other variable (for example its own token or cloud keys). `aws:default-chain` is not an environment read: Bedrock calls go to AWS only, signed by the worker's default boto3 chain.

Fetch server. Anything an agent can fetch is also a place it can send data (a URL can carry conversation text). The shipped `fetch` manifest therefore lists a few documentation hosts instead of `*`, and `maef lint` warns on `*`. Widen the list only for tasks that need it.
