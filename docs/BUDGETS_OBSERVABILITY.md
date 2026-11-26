# Budgets, monitoring and observability

## Budget model
The unit is the integer token (`input_tokens + output_tokens` as the provider reports them). A run has three ceilings: `max_tokens`, `max_duration_seconds` and `max_turns` (`models.py` `Budget`). A request can only tighten the agent's ceiling, never raise it (`BudgetOverride`, `runs.py`). There is no per-tool, USD, CPU-second, storage or network ceiling; `cost_usd` is provider-reported information and the ledger never reads it for admission.

Not implemented (design intent only): separate `model_spend_usd` and `tool_spend_usd` ceilings, decimal money accounting, an organisation policy layer, pricing profiles, blocking unpriced endpoints, and a shared account quota across organisations. There is one budget account per ledger.

## Admission and accounting
1. Admission reserves `min(max_tokens, worst-case bound)` atomically in one transaction and refuses with `insufficient_budget` when the account's free tokens are fewer (`ledger.py` admission; `BEGIN IMMEDIATE` on SQLite, row lock on Postgres).
2. Before each model call the harness checks that `tokens_used + prompt_bytes + max_output_tokens` fits the reservation, and stops on cancellation, turns, duration or tokens (`guard.py`). Prompt bytes bound prompt tokens because a token is at least one byte.
3. The worker records `usage.recorded` events per call. On finish the ledger charges `min(max(reported, recorded), reservation)` and releases the rest (`ledger.py` `_settle`). A figure above the reservation is clamped and a `budget.overreport` event records reported, recorded, reserved and charged tokens.
4. When a lease expires the run fails as `worker_lost` and is charged the usage the worker recorded, or the whole reservation if it recorded none (`ledger.py` `expire_leases`).
5. Cancellation stops the next model call; a call already sent to the provider may still bill.
6. `maef admin account reset` starts a new period (`ledger.py` `reset_account`; refused while tokens are reserved unless forced; audited).

Not implemented: distinguishing observed, calculated and estimated charges, and delayed-usage reconciliation. An absolute ceiling is not guaranteed even for tokens: the guard bounds a call before it is sent, but the provider's own count can exceed the bound (the clamp then caps what the ledger charges, not what the provider bills).

## Run endings
Every terminal run carries a reason and a class, `agent` (the agent's own outcome, such as `tokens_exhausted`) or `infrastructure` (a loss, such as `worker_lost`, `provider_error`, `sandbox_error`, `artifact_error`, `mcp_manifest_mismatch`). Benchmarks list infrastructure losses apart from the score (`endings.py`). The worker retries sends to the API until the lease deadline, so a short API outage does not end a run.

## Events per run
The ledger emits these types (`run_events`, per-run gapless `seq`): `task.created`, `task.admitted`, `run.created`, `run.manifest`, `run.started`, `lease.granted`, `lease.expired`, `sandbox.provisioned`, `sandbox.torn_down`, `model.request`, `model.response`, `model.error`, `tool.call`, `tool.result`, `usage.recorded`, `budget.overreport`, `artifact.stored`, `run.output`, `run.rejected`, `run.internal_error`, `task.succeeded`, `task.failed`, `task.cancelled`, plus `decision.request` on decision runs and `transcript.recorded` or `transcript.failed` when the transcript is on. Each name is checked against the source by `tests/test_docs_claims.py`.

Each event has `seq`, `type`, UTC `ts` and a `payload`; the event stream frame is exactly those four fields (`main.py` `_frame`). The run id is the URL, not a field. Agent id, agent revision, worker id and trace or span id are in `run.created`, `run.manifest` and `lease.granted` payloads or derived by `otel.run_to_spans`, not repeated on every event. Events carry sizes and token counts, not prompts; the full conversation is stored only when `MAEF_RECORD_TRANSCRIPT=1`, and it is not redacted.

## Metrics
What to measure. Only the Implemented section below is built; the rest is a wish list.

- Success/failure/cancellation: implemented per run and in `/api/v1/metrics`, not labelled by agent revision.
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
Implemented: live event stream over SSE (`GET /api/v1/runs/{id}/events`), run listing, artefact download, an append-only `audit_log` that lives in the ledger and so survives worker deletion. Not implemented: run comparison and cost views in Studio, and log redaction. The list above under Metrics is design intent except where the Implemented section below says otherwise.

## Implemented (BACKEND_PLAN step 14)
- **Traces:** `otel.run_to_spans(run)` maps a run's ledger events to OpenTelemetry-shaped spans (mapping in `packages/contracts/CONTRACTS.md`). Ids are sha256 truncations of run id and event seq, so re-export is idempotent. Model spans also carry `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens`; the `maef.run` span carries their sums. Model spans also carry `maef.usage.reasoning_tokens`, `maef.usage.cost_usd`, `gen_ai.response.id`, `gen_ai.response.model`, `maef.provider` and `maef.latency_ms` when known (the `maef.run` span carries the token and cost sums), and tool spans carry `maef.tool.duration_ms`. Spans carry identifiers, sizes and token counts only, never prompt, argument or result content (the events do not contain any).
- **Export:** `maef otel export --endpoint URL [--since ISO] [--run ID]` (reads through `MaefClient`, so it needs a `read` token) and an optional background exporter in the API: with `MAEF_OTLP_ENDPOINT` set, a daemon thread exports each terminal run once (`otel_exports` ledger table); failures are logged and retried on the next 15 s tick and never affect runs. OTLP/HTTP JSON only, through the egress policy: loopback only for a loopback endpoint, private networks only with `MAEF_OTLP_ALLOW_PRIVATE=1` / `--allow-private`. Auth headers come from a credential ref (`MAEF_OTLP_HEADERS_REF=env:NAME`, value `Name=value,Name2=value2`).
- **Transcript (opt-in):** set `MAEF_RECORD_TRANSCRIPT=1` on the worker to store the whole conversation (system prompt, instruction, model text and reasoning, tool arguments and results) as the `transcript.jsonl` artifact. It is off by default because it holds full prompts and tool output. Events still never carry text. Format: `packages/contracts/CONTRACTS.md`.
- **Metrics:** `GET /api/v1/metrics` (scope `read`), Prometheus text, computed by one `Ledger.metrics()` SQL pass. `maef_cached_input_tokens_total{kind=read|write}` counts cached input tokens. It is a subset of `maef_tokens_used_total{kind="input"}`, so do not add the two. `maef_reasoning_tokens_total` is a subset of the output tokens. `maef_cost_usd_total` sums the provider-reported cost (information only).
- **Evals:** `evals/<suite>.yaml`, `maef eval run`, comparison of agent revisions on pass rate and tokens.
- **Not implemented:** latency histograms (queue wait, model latency), per-agent-revision metric labels, OTel metrics export, a cost (USD) view. The model span's `gen_ai.request.model` is the model *profile id* (events do not record the provider model id).
