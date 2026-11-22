# Decision models (BACKEND_PLAN step 9)

Decision models answer typed questions about a `state` (string, object or array) instead of generating text.
MAEF runs them through the same ledger, token budget, worker and artifact pipeline as agents:
`POST /api/v1/decisions` -> queued run (`kind: "decision"`) -> worker calls `DecisionAdapter.decide` once ->
`answers.json` artifact. Wire formats below were read from the sources listed, on 2026-11-22.

## Sources
- decisions-api.dev: https://decisions-api.dev/docs (page text and the `/v1/systemone` snippet on https://decisions-api.dev), model page https://decisions-api.dev/model/clef-omni
- TypeSafe API reference (OpenAPI, same System One schema): https://docs.typesafe.ai/api/system-one (`.md` variant), https://docs.typesafe.ai/models
- Cloudflare Workers AI: https://developers.cloudflare.com/workers-ai/models/clef-omni/ with its raw schemas
  https://developers.cloudflare.com/workers-ai/models/clef-omni/schema-input.json and `schema-output.json`;
  https://developers.cloudflare.com/workers-ai/models/clef/ and `/clef-flash/`;
  REST envelope: https://developers.cloudflare.com/workers-ai/get-started/rest-api/ ; errors: https://developers.cloudflare.com/workers-ai/platform/errors/

## Jev via decisions-api.dev (verified)
`POST https://decisions-api.dev/v1/systemone`, `Authorization: Bearer <key>`, `Content-Type: application/json`,
optional `Idempotency-Key` (8-64 letters, digits, `_`, `-`; reuse returns 409 without another model call; MAEF sends a fresh one per decide).
Body: `{"model": "typesafe/jev-1.13", "state": <string|object|array>, "questions": {name: question}}`.
- Questions: 1-8 per request; ids start with a letter, then letters, digits, `_`, `-`, max 64 chars. Text plus questions must fit 32 KiB (images excluded; images are not sent by MAEF).
- `question`: `{"type": "choice"|"score"|"noul", "instructions": ..., "criteria": ...}`.
  choice: `criteria` = `{option: description}` (2-255 options on the platform). score: `criteria` = ordered list of 2-10 level descriptions (level = index, from 0).
  noul: optional `criteria` = `{"true": ..., "false": ...}`.
- Response (HTTP 200): `{"code": 0, "message": "ok", "data": {"requestId", "creditsUsed", "historySaved", "result": {"model", "answers", "usage": {"input_tokens", "output_tokens"}, "elapsedMs"}}}`. Answers are at `data.result.answers`; failures use non-2xx plus `code != 0`.
- Answers (same shape as TypeSafe's own API):
  noul `{"type":"noul","noul": P(yes) 0..1}`;
  choice `{"type":"choice","choice": <criteria key>,"probabilities":{key:p},"confidence": 0..1}`;
  score `{"type":"score","score": 0..(levels-1) fractional,"legend":{"0":desc,...},"probabilities":{"0":p,...},"confidence": 0..1}`.
- HTTP errors: 400 invalid input, 401 key required, 402 insufficient credits, 409 duplicate request, 413 too large, 429 wait (Retry-After), 502 provider failure, 503 unavailable. MAEF records the status only, never the body.
- Billing: credits per input token (output free); usage tokens are the input/output counts above.
- TypeSafe's own API (`https://api.typesafe.ai/v1/systemone`) documents the same request but an **unwrapped** response `{"model","answers","usage"}`, 400 `{"detail": ...}` and 422 validation errors; MAEF does not ship an adapter for it (the `decisions-api` adapter expects the `code`/`data` envelope). Adding one is a small variant.

## Clef via Cloudflare Workers AI (schemas verified; envelope inferred)
Models: `@cf/cloudflare/clef-omni` (text, JSON, images, audio, video; 64k context), `@cf/cloudflare/clef` and `@cf/cloudflare/clef-flash` (text/JSON/images).
`POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/cloudflare/clef-omni`, `Authorization: Bearer <API token>`.
Body: `{"model": "clef-omni", "state": ..., "questions": {id: question}}` (`model` must match `clef-omni`, or `clef|clef-flash` for those models).
- 1-64 questions (ids: letters, digits, `_`, `.`, `-`, max 100); choice 2-255 options; score 2-10 levels (list); noul optional `criteria.true/false`; `instructions` required, non-empty.
- Optional `images[]` (max 4), `audio[]` (max 4, clef-omni), `videos[]` (max 2, clef-omni) as embedded data. **MAEF does not send media in this step.**
- Output schema (from `schema-output.json`): `{"model", "answers", "usage": {"input_tokens", "output_tokens"}}`, answers identical in shape to the Jev answers above (noul/choice/score, with `legend` on score).
- Long text `state` is truncated by the model to fit its token limit (silent).
- Errors: https://developers.cloudflare.com/workers-ai/platform/errors/ (HTTP status plus internal code).
- Gap: the docs show the generic Workers AI REST envelope `{"result": {...}, "success": true, "errors": [], "messages": []}` (documented with a different model) but no clef-specific REST response example. The adapter unwraps `result` when present (requiring `success: true`) and also accepts an unwrapped body. Not exercised against the live API (no account in this environment).
- Clef is also listed on decisions-api.dev as `cloudflare/clef-omni`; use a `decisions-api` profile with that `model_id` to reach it through the platform instead.

## MAEF seam (`adapters/decision.py`)
- `DecisionRequest(state, questions)`: 1-8 questions, names `^[a-z][a-z0-9_]{0,40}$`, serialized state <= 64 KiB; `Question(type, instructions<=2000, criteria)` with choice 2-20 options `{option: description}`, score 2-10 ordered levels, noul optional `{true,false}` (the intersection of both providers' limits).
- `Answer(type, value, confidence?, probabilities?)`: value is the chosen option (choice), the 0-based fractional level (score) or P(yes) (noul). `Decision(answers, input_tokens, output_tokens, model)`.
- Strict validation (`invalid_decision`, no body echoed): every requested question answered and none extra, `type` matches, choice value is a criteria key, score within 0..levels-1, noul/confidence/probabilities within 0..1, probability keys within the option/level set.
- `DecisionsApiAdapter` (profile kind `decisions-api`, dialect `decisions-api`, default base_url `https://decisions-api.dev`), `WorkersAiDecisionAdapter` (kind `cloudflare-workers-ai`, dialect `workers-ai-decision`, `account_id` 32 hex, `model_id` `@cf/<vendor>/<model>`, default base_url `https://api.cloudflare.com/client/v4`), `FakeDecisionAdapter`. They reuse `post_json` (egress policy, no redirects, bounded retries) and `Credentials`.
- Metering: the run reserves `min(max_tokens or 50000, request bytes + 1024)` tokens (a token is at least one byte). The worker charges the provider-reported tokens; if none are reported it charges the request bytes as input tokens; an `invalid_decision` response (the provider answered) is also charged the request bytes. Other errors charge 0 like agent model errors.
- Events: `run.started`, `decision.request {profile, questions: [names], state_bytes}`, `usage.recorded`, `artifact.stored`, `run.output`, `run.manifest`, terminal `task.*`. State and answers never appear in events; answers are only in the `answers.json` artifact (`{"answers": {name: Answer}}`) and are untrusted content.

## Known gaps
- No image/audio/video state (Jev 1.14 and Clef accept images; not verified end to end here, not sent).
- Clef REST envelope inferred (above); no live call made against either provider.
- decisions-api retries reuse one Idempotency-Key, so a retry after an ambiguous failure can come back 409 (fails closed, no double charge) rather than succeeding.
- The `decisions-api` request cap is 32 KiB for text plus questions (checked by the adapter), below MAEF's 64 KiB state limit.
- No dedicated adapter for TypeSafe's own `api.typesafe.ai` response shape.
