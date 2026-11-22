# Proposed versioned contracts

## AgentDefinition (implemented v1alpha1)
`schema_version`, `id`, `version`, `display_name`, `description`, `capabilities[]`, `instructions` path, `skills[]` paths, `mcp_servers[]` logical IDs, `runtime` (kind, model_profile, sandbox_profile), `budget` and `outputs[]`.

## ModelEndpointProfile (implemented v1alpha1)
One file per profile in `model-profiles/<id>.yaml` (gitignored: profiles are local, per-machine configuration), served at `GET /api/v1/model-profiles[/{id}]`. Fields: `schema_version`, `id`, `version`, `display_name`, `vendor`, `api_dialect` (`bedrock-converse`, `openai-compatible`, `anthropic`, `cli-adapter`), `endpoint` (`kind: amazon-bedrock`, `region`, `model_id`, `routing`: `in-region` | `geo` | `global`), `credential_ref` (a reference such as `aws:default-chain`, never a secret), `capabilities` (`input_modalities[]`, `streaming`), `timeout_seconds`, `max_retries`, `price_schedule_id` (`null` = unpriced, so admission fails closed). Unknown fields are rejected so a stray `api_key` cannot be committed into a profile.

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
