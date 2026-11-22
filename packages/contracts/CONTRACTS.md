# Proposed versioned contracts

## AgentDefinition (implemented v1alpha1)
`schema_version`, `id`, `version`, `display_name`, `description`, `capabilities[]`, `instructions` path, `skills[]` paths, `tools[]` built-in tool names (`current_time`, `read_agent_file`; unknown names fail `Definitions.resolve`), `mcp_servers[]` logical IDs, `runtime` (kind, model_profile, sandbox_profile), `budget` and `outputs[]`.

## ModelEndpointProfile (implemented v1alpha1)
One file per profile in `model-profiles/<id>.yaml` (gitignored: profiles are local, per-machine configuration), served at `GET /api/v1/model-profiles[/{id}]`. Fields: `schema_version`, `id`, `version`, `display_name`, `vendor`, `api_dialect` (`bedrock-converse`, `openai-compatible`, `anthropic`, `cli-adapter`), `endpoint` (`kind: amazon-bedrock`, `region`, `model_id`, `routing`: `in-region` | `geo` | `global`), `credential_ref` (a reference such as `aws:default-chain`, never a secret), `capabilities` (`input_modalities[]`, `streaming`), `timeout_seconds`, `max_retries`, Unknown fields are rejected so a stray `api_key` cannot be committed into a profile.

## TaskSubmission (preview implemented)
`agent_id`, `instruction`, optional `budget`. Future: `agent_revision`, `context_refs`, `artifact_targets`, `priority`, `idempotency_key`, `approval_policy`, `worker_class`.

## WorkerRegistration (planned)
`worker_id`, `worker_version`, `architecture`, `capabilities[]`, `sandbox_profiles[]`, `capacity`, `heartbeat_interval_seconds`.

## ExecutionLease (planned)
`lease_id`, `fencing_token`, `run_id`, `expires_at`, `agent_git_revision`, `definition_digest`, `approved_policy_digest`, `runtime_profile`, `budget_reservation_id`, `scoped_credential_refs`.

## ExecutionEvent (planned)
`run_id`, `seq`, `event_type`, `timestamp`, `trace_id`, `payload` (typed by event type).

## ResultManifest (planned)
`run_id`, `status`, `agent_revision`, `model_endpoint_profile_revision`, `tool_versions`, `context_digests`, `usage`, `cost_provenance`, `artifacts` (URI, digest, MIME, size), `git_branch`, `git_commit`, `validation_results`, `failure_reason`.

## Compatibility policy
Schemas carry explicit API versions. Unknown fields should be rejected on security-critical command contracts; preserve backward compatibility for at least one minor release once public. Worker/controller capability negotiation must prevent unsupported policy downgrades.


## PinnedDefinition (implemented, internal)
An agent and its model profile pinned for a run (`Definitions.resolve`): `agent`, `profile`, `instructions` (decoded from the digested bytes), `git_rev`, `dirty`, `definition_digest` (sha256 over agent.yaml, instructions and skills), `profile_digest`. Unknown git state counts as dirty.

## Changes in this revision
`Budget.max_cost_usd` was replaced by `max_tokens` (total input + output tokens); there are no price schedules. `Runtime.kind` was removed: the dialect comes from the model profile.

## ExecutionEvent, ResultManifest, run ledger (implemented)
SQLite ledger (`runs/maef.db`, override with `MAEF_DB`). `run_events` is append-only (triggers reject UPDATE/DELETE) with gapless `seq` per run. Usage is integer tokens as reported by the provider. Admission reserves `min(max_tokens, worst-case tokens)` in one `BEGIN IMMEDIATE` transaction and fails closed on dirty or unknown git state (dirty allowed only with an explicit flag, recorded in the `task.created` event). `settle` charges the actual tokens and returns the remainder. Read-only `GET /api/v1/runs` and `GET /api/v1/runs/{id}`.

## Execution (implemented, interim)
`POST /api/v1/runs` returns 202 `{run_id}` when `MAEF_EXECUTION_ENABLED=1` (otherwise 501), runs in a local thread pool, never in the request. A turn loop (see below) over the profile's `max_output_tokens`, timeout and retries. Events: `run.started`, per turn `model.request` (`turn`, `prompt_bytes`, `tools`), `model.response` (`turn`, `stop_reason`, `tool_calls`), `usage.recorded` (`turn`, tokens), `tool.call` (`turn`, `name`, `argument_bytes`), `tool.result` (`turn`, `name`, `result_bytes`, `is_error`), then `run.output`, `run.manifest`, then the terminal `task.*` event. Startup sweep fails orphaned runs as `worker_lost` and charges their full reservation. In-process execution is an interim exception to ADR-0002 that ends when tools arrive.

## Endpoint kinds (implemented)
`endpoint.kind`: `amazon-bedrock`, `openai-compatible` (OpenAI or any local chat/completions server; `base_url`, `token_param`) and `anthropic` (Messages API). `api_dialect` must match the kind. `base_url` must be https, or http on loopback only, with no embedded credentials. `credential_ref` is `aws:default-chain`, `none:local` or `env:NAME`; the key is read from the environment at call time and never stored or logged. HTTP errors record status and class only, never bodies. A missing `usage` block fails the call, since the budget cannot be metered. Other endpoint kinds plug in by adding a kind and an adapter.

## Credentials and egress (implemented)
`Credentials.resolve(credential_ref)` returns an opaque `Secret` (`reveal()` is the only way to read it; repr/str/format print `Secret(***)`) or `None` for `none:local` and `aws:default-chain` (Bedrock uses the boto3 default chain itself). A missing or empty `env:NAME` raises `CredentialError("missing_credential:NAME")`, naming the variable only. Every HTTP model call passes through `EgressPolicy.check` before each attempt: all resolved addresses must be allowed; unspecified, multicast, reserved and link-local (including 169.254.169.254, fe80::/10, fd00:ec2::254) are always denied, IPv4-mapped/compatible IPv6 is evaluated as the embedded v4, loopback is allowed only when the profile host is loopback, and private ranges (RFC1918, 100.64/10, fc00::/7) only with `endpoint.allow_private_network: true`. Plain `http` base URLs require a loopback host or that flag. Redirects are not followed. Known gap: DNS is resolved again by the HTTP client at connect time (rebinding TOCTOU).


## Conversation, tools and budget guard (implemented)
Neutral types in `adapters/base.py`: `ToolSpec(name, description, input_schema)`, `ToolCall(id, name, arguments, parse_error)`, `Message(role: user|assistant|tool, text, tool_calls, tool_call_id, is_error)`, `ModelResponse(text, tool_calls, stop_reason, input_tokens, output_tokens)`. `stop_reason` is normalised to `end_turn` | `tool_use` | `max_tokens` | `other:<raw>`. `ModelAdapter.converse(system, messages, tools, max_output_tokens, timeout_seconds)` is one non-streaming call; each adapter (openai-compatible, anthropic, bedrock-converse) translates to and from its wire format. The openai-compatible adapter tolerates local servers (Ollama, LM Studio): `arguments` as a string or object, missing tool call ids (`call_<index>`), `finish_reason: stop` with tool calls (treated as `tool_use`), null content, and omits `tools` when there are none. Unparseable arguments keep the call with `{}` and `parse_error`, and the harness returns an error tool result without invoking.

`BudgetGuard(budget, reserved_tokens, max_output_tokens)` (`guard.py`): `check(prompt_bytes)` returns `turns_exhausted`, `duration_exhausted`, `tokens_exhausted` (`tokens_used + prompt_bytes + max_output_tokens > reserved_tokens`) or `None`; `record(input, output)` after each call. A stop reason ends the run `failed` with that terminal reason. Prompt bytes include the system prompt, message text, tool call arguments and tool definitions, and admission counts the tool definitions too.

`ToolSet` (`tools.py`): `specs()` and `invoke(name, arguments) -> ToolResult(content, is_error)`, which never raises. `BuiltinTools` provides read-only `current_time` and `read_agent_file` (`{"path"}`: only the agent's instructions file and listed skills; no symlinks or traversal). Results are capped at 32 KiB with a truncation marker. Events never contain argument or result contents. Terminal reasons: `completed`, `output_truncated`, `unsupported_stop_reason:<x>`, `malformed_response`, `model_error:<Class>` and the guard reasons above.
