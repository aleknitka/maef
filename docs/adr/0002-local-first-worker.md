# ADR-0002 — Local-first, remote-ready worker architecture
Status: Accepted as target design (2026-11-21)

Decision: Use independently deployable workers and outbound-authenticated lease protocol. Start with one worker on local Linux with rootless Docker. Use capability-based scheduling, digests and explicit sandbox profiles. No Kubernetes or Temporal until operational complexity warrants either.

Alternatives: in-process subprocess workers (too much coupling); Kubernetes Jobs (too much local complexity); serverless only (unsuitable for sustained rendering and arbitrary tools).

Consequences: Additional worker/control communication design now; straightforward remote VM support later. Linux-first execution is intentional; macOS/Windows workers require separate validation of isolation guarantees.
