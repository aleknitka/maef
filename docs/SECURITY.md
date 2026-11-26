# Threat model and security requirements

## Trust boundaries
(1) Browser -> control-plane API; (2) control plane -> worker; (3) worker -> sandbox; (4) sandbox -> model endpoint/tool/MCP; (5) worker -> artifact store; (6) untrusted Git/skill/tool source -> runtime.

## Minimum controls before execution is enabled
- Authentication, authorisation and per-action audit logs for any network-accessible deployment. **Met (step 11):** scoped bearer tokens on every `/api/v1/*` route, per-worker credentials with lease fencing by worker, append-only `audit_log`. Remaining: TLS termination is the operator's job; tokens have no expiry yet.
- Immutable Git definition and dependency resolution; signed/approved registries where feasible.
- Capabilities granted explicitly per agent + task, scoped to filesystem, network and secrets.
- No arbitrary docker daemon socket mounted into generated-code containers.
- Container resource limits and bounded execution time, process counts and storage allocation.
- Rootless worker supervisor and policy-aware sandbox; egress restrictions verified by integration tests.
- Secrets provided through a broker and redacted from logs, traces and artefacts.
- Workspace attachment policy guards symlinks, path traversal and host path injection.
- Explicit approval for destructive actions, external side effects, publishing, merging or escalation.
- Per-tool instrumentation and accounting; deny launch if budget and pricing policy cannot be evaluated.
- Endpoint allowlist and protection against SSRF, including private network metadata endpoints. (Met for model endpoints in step 1. Met for MCP servers with `network: allowlist` in step 11b: internal-only network, enforced proxy, host/port allowlist, public-address-only, connect-to-vetted-IP. `network: open` remains unfiltered and discouraged.)

## Additional concerns
MCP server descriptions are not trusted instructions. An MCP tool may be compromised or return malicious text. Use signature/pinning and approvals; do not inherit all parent MCP configuration. Even rootless containers share a host kernel. For hostile third-party code, evaluate gVisor/Firecracker microVMs or remote hosts with stronger separation. Limit access to local files and cloud metadata credentials.

## Desktop constraints
On macOS and Windows Docker usually executes inside a VM; Linux execution semantics differ. Record worker OS, architecture and isolation features, and refuse incompatible sandbox profiles rather than silently downgrading security.

## Review process
A definition change is reviewed as code: show prompt diff, tool-permission diff, newly requested domains/secrets, tests and benchmark results before approving/publishing. Version changes to agent instructions and MCP manifests trigger re-review.

Worker credentials. The worker resolves an `env:NAME` reference only when NAME appears in the project's own `model-profiles/` or `mcp-servers/` files or in `MAEF_WORKER_ALLOWED_CREDENTIALS`; a lease cannot make it read any other variable (for example its own token or cloud keys). `aws:default-chain` is not an environment read: Bedrock calls go to AWS only, signed by the worker's default boto3 chain.

Fetch server. Anything an agent can fetch is also a place it can send data (a URL can carry conversation text). The shipped `fetch` manifest therefore lists a few documentation hosts instead of `*`, and `maef lint` warns on `*`. Widen the list only for tasks that need it.
