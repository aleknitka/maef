"""Build and render the benchmark report from events, transcript analysis and the hidden-case grade."""

import subprocess
from datetime import datetime

from ..registry import ROOT
from .families.base import STRAPLINE

_NA = "not reported"


def _ts(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def git_revision() -> str:
    try:
        done = subprocess.run(  # noqa: S603  argv list, no shell; fixed container-runtime binary, no host shell
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],  # noqa: S607  fixed 'git' executable resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return done.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def wall_seconds(events: list[dict]) -> float | None:
    stamps = [t for e in events if (t := _ts(e.get("ts"))) is not None]
    return round(max(stamps) - min(stamps), 2) if len(stamps) >= 2 else None


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def usage_by_turn(events: list[dict]) -> dict[int, dict]:
    """Merge usage.recorded, model.response and (for latency) tool.result events per turn."""
    turns: dict[int, dict] = {}
    for e in events:
        p = e.get("payload") or {}
        turn = p.get("turn")
        if not isinstance(turn, int):
            continue
        row = turns.setdefault(turn, {"turn": turn})
        if e.get("type") == "usage.recorded":
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "cost_usd",
            ):
                if _num(p.get(k)) is not None:
                    row[k] = p[k]
        elif e.get("type") == "model.response":
            for k in ("latency_ms", "provider", "model", "response_id"):
                if p.get(k) is not None:
                    row[k] = p[k]
    return turns


def _sum(rows, key):
    vals = [_num(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def build(
    *,
    run: dict,
    events: list[dict],
    rows: list[dict],
    trace: dict | None,
    grade: dict,
    variant,
    family,
    canonical,
    agent_id: str,
    artifact_names: list[str],
) -> dict:
    ev_turns = usage_by_turn(events)
    timeline = {t["turn"]: dict(t) for t in (trace or {}).get("timeline", []) if t.get("turn") is not None}
    for turn, row in ev_turns.items():  # events fill what the transcript lacks
        merged = timeline.setdefault(turn, {"turn": turn})
        for k, v in row.items():
            if merged.get(k) is None:
                merged[k] = v
    per_turn = [timeline[k] for k in sorted(timeline)]
    assistants = [r for r in rows if r.get("type") == "assistant"]
    models = sorted(
        {str(x) for x in [r.get("model") for r in assistants] + [r.get("model") for r in ev_turns.values()] if x}
    )
    providers = sorted(
        {str(x) for x in [r.get("provider") for r in assistants] + [r.get("provider") for r in ev_turns.values()] if x}
    )
    cost = _sum(per_turn, "cost_usd")
    inp, cached, out, reas = (
        _sum(per_turn, k) for k in ("input_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens")
    )
    total = None if inp is None and out is None else (inp or 0) + (out or 0)
    expected = family.deliverables(variant) if hasattr(family, "deliverables") else []
    names = {n.rsplit("/", 1)[-1] for n in artifact_names}
    return {
        "run_id": run.get("run_id") or run.get("id"),
        "agent_id": agent_id,
        "git_revision": git_revision(),
        "agent_revision": run.get("agent_revision") or run.get("definition_digest"),
        "family": variant.family,
        "seed": variant.seed,
        "noise": variant.noise,
        "variant": variant.model_dump(),
        "canonical": canonical.model_dump(),
        "status": run.get("status"),
        "terminal_reason": run.get("terminal_reason"),
        "models": models,
        "providers": providers,
        "wall_seconds": wall_seconds(events),
        "turns": (trace or {}).get("turns") or len(per_turn),
        "tool_calls": (trace or {}).get("tool_calls"),
        "tool_calls_by_kind": (trace or {}).get("tool_calls_by_kind"),
        "tokens": {"input": inp, "cached": cached, "output": out, "reasoning": reas, "total": total},
        "cost_usd": cost,
        "per_turn": per_turn,
        "tdd": trace,
        "hidden": grade,
        "deliverables": {
            "expected": expected,
            "present": [n for n in expected if n in names],
            "missing": [n for n in expected if n not in names],
        },
    }


def _f(x, nd=None):
    if x is None:
        return "-"
    return f"{x:.{nd}f}" if nd is not None and isinstance(x, float) else str(x)


def render(r: dict) -> str:
    t, h, tok, d = r.get("tdd"), r["hidden"], r["tokens"], r["deliverables"]
    cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else _NA
    lines = [
        f"# Benchmark report: {r['family']} seed {r['seed']} noise {r['noise']}",
        "",
        f"> {STRAPLINE}",
        "",
        f"- Run: `{r['run_id']}`",
        f"- Agent: `{r['agent_id']}` (repo revision `{r['git_revision']}`"
        + (f", definition `{r['agent_revision']}`" if r.get("agent_revision") else "")
        + ")",
        f"- Model: {', '.join(r['models']) or _NA}; provider: {', '.join(r['providers']) or _NA}",
        f"- Status: {r['status']}" + (f" ({r['terminal_reason']})" if r.get("terminal_reason") else ""),
        f"- Wall time: {_f(r['wall_seconds'])} s",
        (f"- Turns: {r['turns']}; tool calls: {_f(r['tool_calls'])} {r['tool_calls_by_kind'] or ''}").rstrip(),
        (
            f"- Tokens: input {_f(tok['input'])}, cached {_f(tok['cached'])}, output {_f(tok['output'])}, "
            f"reasoning {_f(tok['reasoning'])}, total {_f(tok['total'])}"
        ),
        f"- Cost: {cost}",
        "",
        "## Per-turn",
        "",
        "| turn | t (s) | latency ms | in | cached | out | reasoning | cost | tool calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for p in r["per_turn"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _f(p.get("turn")),
                    _f(p.get("t"), 1),
                    _f(p.get("latency_ms")),
                    _f(p.get("input_tokens")),
                    _f(p.get("cache_read_tokens")),
                    _f(p.get("output_tokens")),
                    _f(p.get("reasoning_tokens")),
                    _f(p.get("cost_usd")),
                    _f(p.get("tool_calls")),
                ]
            )
            + " |"
        )
    lines += ["", "## TDD analysis", ""]
    if t is None:
        lines.append("No transcript was available, so no TDD analysis.")
    else:
        lines += [
            f"- **Verdict: {t['verdict']}**",
            (
                f"- First test file written: turn {_f(t['first_test_file_write'])}; first implementation written: "
                f"turn {_f(t['first_impl_write'])}; tests written first: {t['tests_written_first']}"
            ),
            (
                f"- First test run: turn {_f(t['first_test_run_turn'])}, failed first (red): "
                f"{_f(t['first_test_run_failed'])}"
            ),
            (
                f"- Red to green cycles: {t['red_to_green_cycles']}; turns to first green: "
                f"{_f(t['turns_to_first_green'])}; final tests green: {_f(t['final_tests_green'])}"
            ),
            "- Rewrites per file: " + (", ".join(f"{p} x{n}" for p, n in t["rewrites"].items()) or "none"),
            "",
            "| test run turn | passed | ran | failures | errors |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {x['turn']} | {x['passed']} | {x['ran']} | {x['failures']} | {x['errors']} |" for x in t["test_runs"]
        ]
    lines += ["", "## Hidden acceptance", ""]
    if h.get("error"):
        lines.append(f"Grading problem: {h['error']}")
    else:
        lines.append(f"**{h['passed']} / {h['total']} passed** ({h['failed']} failed)")
        for e in h["errors"][:20]:
            lines.append(f"- `{e.get('case')}`: expected {e.get('expected')!r}, got {e.get('got')}")
    final = t["final_test_result"] if t else None
    lines += [
        "",
        "## The agent's own tests",
        "",
        (
            f"Last run at turn {final['turn']}: {'passed' if final['passed'] else 'FAILED'} "
            f"(ran {final['ran']}, failures {final['failures']}, errors {final['errors']})"
        )
        if final
        else "The agent never ran its tests.",
        "",
        "## Deliverables",
        "",
        f"- Present: {', '.join(d['present']) or 'none'}",
        f"- Missing: {', '.join(d['missing']) or 'none'}",
        "",
        "## Variant versus canonical",
        "",
        "| parameter | this variant | canonical |",
        "|---|---|---|",
    ]
    v, c = r["variant"], r["canonical"]
    for k in sorted({*v["params"], *c["params"]}):
        lines.append(f"| {k} | {v['params'].get(k)} | {c['params'].get(k)} |")
    for k in sorted({*v["names"], *c["names"]}):
        lines.append(f"| name: {k} | {v['names'].get(k)} | {c['names'].get(k)} |")
    return "\n".join(lines) + "\n"
