# ADR-0004 — Budget admission and explicit limits
Status: Proposed, requires end-to-end verification

Decision: Multiple independent caps (USD, tokens, turns, time, CPU), atomic reservations before model calls, fail-closed for unpriced external endpoints and reconcile actual provider usage. The task may reduce, never increase, policy ceilings. CLI subscription-based adapters cannot claim API-style cost enforcement when detailed usage is missing.

Risk: No general system can promise exact hard spending caps across delayed external billing and uninterruptible in-flight calls. The product must accurately expose best-effort ceilings and accounting provenance.
