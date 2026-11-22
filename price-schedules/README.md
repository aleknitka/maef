# Price schedules

One `<id>.yaml` per schedule, referenced by a model profile's `price_schedule_id`. Prices are USD per million tokens and are not secrets, so these files are committed. A profile with no schedule is unpriced and budget admission rejects it (ADR-0004).

```yaml
schema_version: maef.dev/v1alpha1
id: example-schedule
effective_from: 2026-01-01
input_usd_per_million_tokens: "1.000000"
output_usd_per_million_tokens: "5.000000"
```

Copy current prices from the provider's pricing page; MAEF does not ship them.
