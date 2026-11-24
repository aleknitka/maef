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
- Token normalisation: `input_tokens` is every prompt token processed, including cache reads and cache writes. `cache_read_tokens` and `cache_write_tokens` are the parts of it served from or written to the prompt cache. Anthropic and Bedrock report input excluding cache tokens, so the adapters add them back; OpenAI-style providers already include them. Budgets still charge `input_tokens + output_tokens`.
- Reasoning and cost: `reasoning_tokens` is the part of `output_tokens` spent on hidden reasoning (a subset, like the cache fields; OpenAI-style `completion_tokens_details.reasoning_tokens`). `cost_usd` is the cost the provider reported (OpenRouter sends `usage.cost`); it is information only and provider-reported, so it can be missing. Budgets stay token-only. `model.response` also carries `latency_ms` (the model call's wall time) and, when the provider sends them, `provider`, `response_id` and `model`; `tool.result` carries `duration_ms`. A profile can set `endpoint.reasoning_effort` (`minimal|low|medium|high`) to send `reasoning_effort` on OpenAI-style calls.
- MAEF does not yet request caching. Anthropic and Bedrock need explicit cache markers; OpenAI-style providers cache long prompts automatically. Until markers are added, those two will mostly report 0.
- Spent/reserved USD, cost per successful artefact, budget exhaustions.
- Tool invocations, errors, retries and timeouts.
- Worker utilisation, memory/CPU peaks and crash/restart rates.
- Validation outcomes and human approval rates.

Use OpenTelemetry GenAI semantic conventions where stable and pin semantic convention package/version. Their metrics evolve; do not hardcode a provisional name without versioning.

## Visibility
Studio should support structured timelines, live stream of task events (SSE initially), run comparison, cost breakdown, logs with redaction and direct links to artefacts. An audit trail must survive worker deletion.

## Implemented (BACKEND_PLAN step 14)
- **Traces:** `otel.run_to_spans(run)` maps a run's ledger events to OpenTelemetry-shaped spans (mapping in `packages/contracts/CONTRACTS.md`). Ids are sha256 truncations of run id and event seq, so re-export is idempotent. Model spans also carry `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens`; the `maef.run` span carries their sums. Model spans also carry `maef.usage.reasoning_tokens`, `maef.usage.cost_usd`, `gen_ai.response.id`, `gen_ai.response.model`, `maef.provider` and `maef.latency_ms` when known (the `maef.run` span carries the token and cost sums), and tool spans carry `maef.tool.duration_ms`. Spans carry identifiers, sizes and token counts only, never prompt, argument or result content (the events do not contain any).
- **Export:** `maef otel export --endpoint URL [--since ISO] [--run ID]` (reads through `MaefClient`, so it needs a `read` token) and an optional background exporter in the API: with `MAEF_OTLP_ENDPOINT` set, a daemon thread exports each terminal run once (`otel_exports` ledger table); failures are logged and retried on the next 15 s tick and never affect runs. OTLP/HTTP JSON only, through the egress policy: loopback only for a loopback endpoint, private networks only with `MAEF_OTLP_ALLOW_PRIVATE=1` / `--allow-private`. Auth headers come from a credential ref (`MAEF_OTLP_HEADERS_REF=env:NAME`, value `Name=value,Name2=value2`).
- **Transcript (opt-in):** set `MAEF_RECORD_TRANSCRIPT=1` on the worker to store the whole conversation (system prompt, instruction, model text and reasoning, tool arguments and results) as the `transcript.jsonl` artifact. It is off by default because it holds full prompts and tool output. Events still never carry text. Format: `packages/contracts/CONTRACTS.md`.
- **Metrics:** `GET /api/v1/metrics` (scope `read`), Prometheus text, computed by one `Ledger.metrics()` SQL pass. `maef_cached_input_tokens_total{kind=read|write}` counts cached input tokens. It is a subset of `maef_tokens_used_total{kind="input"}`, so do not add the two. `maef_reasoning_tokens_total` is a subset of the output tokens. `maef_cost_usd_total` sums the provider-reported cost (information only).
- **Evals:** `evals/<suite>.yaml`, `maef eval run`, comparison of agent revisions on pass rate and tokens.
- **Not implemented:** latency histograms (queue wait, model latency), reasoning token counts, per-agent-revision metric labels, OTel metrics export, a cost (USD) view. The model span's `gen_ai.request.model` is the model *profile id* (events do not record the provider model id).
