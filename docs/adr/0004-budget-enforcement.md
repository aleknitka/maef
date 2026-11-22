# ADR-0004 — Budget admission and explicit limits
Status: Proposed, requires end-to-end verification

Decision: Multiple independent caps (USD, tokens, turns, time, CPU), atomic reservations before model calls, fail-closed for unpriced external endpoints and reconcile actual provider usage. The task may reduce, never increase, policy ceilings. CLI subscription-based adapters cannot claim API-style cost enforcement when detailed usage is missing.

Risk: No general system can promise exact hard spending caps across delayed external billing and uninterruptible in-flight calls. The product must accurately expose best-effort ceilings and accounting provenance.

## Addendum (token budgets first)
Enforcement is now metered in **tokens** (input + output as reported by the provider), reserved atomically and settled against actual usage. USD ceilings and price schedules were removed: tokens are exact, provider-neutral and always reported, so the fail-closed rule becomes "a call without a usage report fails". Dollar caps can return later as an optional second ceiling for billed APIs.
