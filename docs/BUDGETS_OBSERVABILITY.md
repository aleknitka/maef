# Budgets, monitoring and observability

## Budget model
Keep separate ceilings for `model_tokens`, `model_spend_usd`, `tool_spend_usd`, `wall_time_s`, `turns`, `sandbox_cpu_seconds`, and optional storage/network. All costs must use decimal fixed-point accounting, not floating-point money in the production ledger. Budget effective limits = minimum of organisation policy, agent maximum and task request. Never allow task requests to elevate permissions.

## Admission and accounting
1. Resolve pricing from pinned model/tool pricing profile; block unknown/unpriced billable endpoints unless approved as unmetered.
2. Estimate and reserve a worst-case or configured per-request upper bound *atomically* before dispatch.
3. A task receives a reservation; concurrent tasks share an account/organisation quota ledger.
4. Update spent/reserved after model responses; distinguish observed, calculated and estimated charges.
5. Stop admitting new requests when remaining quota is insufficient; cancellation attempts to stop active calls, although provider-side completion may still produce billable usage.
6. Release unused reservations, reconcile delayed usage, and report possible overshoot explicitly.

An absolute monetary ceiling is not technically guaranteed for all external services: provider timing, out-of-band tool charges and usage reports can lag. Budget semantics must document these exceptions.

## Events per run
`task.created`, `task.admitted`, `task.leased`, `sandbox.provisioned`, `harness.started`, `model.request`, `model.response`, `tool.invoked`, `tool.completed`, `usage.recorded`, `artifact.published`, `validation.completed`, `task.finished`.

Each event includes `task_id`, `run_id`, `agent_id`, `agent_revision`, `worker_id`, monotonically increasing `seq`, UTC timestamp and trace/span identifier. Avoid full prompt logging by default; explicit opt-in only with retention rules.

## Metrics
- Success/failure/cancellation by agent version and runtime.
- Queue wait, sandbox start time, execution wall time, model latency.
- Input/output/cache/reasoning token counts where providers expose them.
- Spent/reserved USD, cost per successful artefact, budget exhaustions.
- Tool invocations, errors, retries and timeouts.
- Worker utilisation, memory/CPU peaks and crash/restart rates.
- Validation outcomes and human approval rates.

Use OpenTelemetry GenAI semantic conventions where stable and pin semantic convention package/version. Their metrics evolve; do not hardcode a provisional name without versioning.

## Visibility
Studio should support structured timelines, live stream of task events (SSE initially), run comparison, cost breakdown, logs with redaction and direct links to artefacts. An audit trail must survive worker deletion.
