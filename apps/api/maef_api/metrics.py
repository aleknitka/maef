"""Prometheus text exposition of the ledger's aggregates (Ledger.metrics()). Pure."""

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
STATUSES = ("admitted", "leased", "running", "succeeded", "failed", "cancelled")


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(m: dict) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[str, int | float]]) -> None:
        lines.extend([f"# HELP {name} {help_}", f"# TYPE {name} {kind}"])
        lines.extend(f"{label} {value}" for label, value in samples)

    runs = dict(m["runs"])
    for status in STATUSES:  # every status for the agent kind is always present
        runs.setdefault((status, "agent"), 0)
    metric("maef_runs_total", "gauge", "Runs by status and kind.", [
        (f'maef_runs_total{{status="{_label(s)}",kind="{_label(k)}"}}', n) for (s, k), n in sorted(runs.items())])
    metric("maef_tokens_used_total", "counter", "Model tokens reported by providers.", [
        (f'maef_tokens_used_total{{kind="{k}"}}', m["tokens"][k]) for k in ("input", "output")])
    metric("maef_cached_input_tokens_total", "counter",
           'Input tokens served from (read) or written to (write) the prompt cache. A subset of '
           'maef_tokens_used_total{kind="input"}; do not add them together.', [
        (f'maef_cached_input_tokens_total{{kind="{k}"}}', m["cached_tokens"][k]) for k in ("read", "write")])
    metric("maef_reasoning_tokens_total", "counter",
           'Output tokens spent on reasoning. A subset of maef_tokens_used_total{kind="output"}; do not add them together.',
           [("maef_reasoning_tokens_total", m.get("reasoning_tokens", 0))])
    metric("maef_cost_usd_total", "counter",
           "Provider-reported cost in USD. Information only; budgets are token-only.",
           [("maef_cost_usd_total", round(m.get("cost_usd", 0.0), 8))])
    metric("maef_tokens_reserved_open", "gauge", "Tokens reserved by runs that have not settled.",
           [("maef_tokens_reserved_open", m["tokens_reserved_open"])])
    metric("maef_run_duration_seconds", "summary", "Wall time of finished runs, created to finished.", [
        ("maef_run_duration_seconds_count", m["duration_count"]),
        ("maef_run_duration_seconds_sum", round(m["duration_sum"], 3))])
    metric("maef_budget_exhaustions_total", "counter", "Runs stopped by an exhausted budget.", [
        (f'maef_budget_exhaustions_total{{reason="{r}"}}', m["exhaustions"].get(r, 0)) for r in ("tokens", "turns", "duration")])
    metric("maef_worker_lost_total", "counter", "Runs failed because their worker lease lapsed.",
           [("maef_worker_lost_total", m["worker_lost"])])
    metric("maef_tool_calls_total", "counter", "Completed tool calls.", [
        (f'maef_tool_calls_total{{is_error="{str(e).lower()}"}}', m["tool_calls"].get(e, 0)) for e in (False, True)])
    metric("maef_artifact_bytes_total", "counter", "Bytes of artifacts recorded for runs.",
           [("maef_artifact_bytes_total", m["artifact_bytes"])])
    return "\n".join(lines) + "\n"
