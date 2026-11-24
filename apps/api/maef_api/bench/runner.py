"""Run a benchmark task through the MAEF API, then grade and report. All results land in out_dir/<run_id>/."""
import json
import time
from pathlib import Path

from ..client import MaefClient
from . import grade, report, trace
from .families import FAMILIES


def _safe_name(name: str) -> Path:
    parts = [p for p in str(name).replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path("unnamed")


def download_artifacts(client: MaefClient, run_id: str, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for ref in client.artifacts(run_id):
        target = dest / _safe_name(ref.get("name", ref.get("digest", "unnamed")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(client.download(ref["digest"]))
        names.append(str(ref.get("name", target.name)))
    return names


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _finish(client: MaefClient, run_id: str, family, variant, cases: list[dict], agent_id: str, run: dict,
            run_dir: Path) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write(run_dir / "task.md", family.render_task(variant))
    _write(run_dir / "variant.json", variant.model_dump_json(indent=2))
    _write(run_dir / "cases.json", json.dumps(cases, indent=2))
    names = download_artifacts(client, run_id, run_dir / "artifacts")
    events = list(client.events(run_id, deadline=time.monotonic() + 30))
    _write(run_dir / "events.jsonl", "".join(json.dumps(e) + "\n" for e in events))
    transcript = run_dir / "artifacts" / "transcript.jsonl"
    rows = trace.load(transcript.read_text(encoding="utf-8", errors="replace")) if transcript.exists() else []
    analysis = trace.analyse(rows) if rows else None
    result = grade.run_grader(family.grader_source(variant), cases, run_dir / "artifacts")
    rep = report.build(run=run, events=events, rows=rows, trace=analysis, grade=result, variant=variant, family=family,
                       canonical=family.sample(0, 0), agent_id=agent_id, artifact_names=names)
    rep["run_id"] = rep["run_id"] or run_id
    _write(run_dir / "report.json", json.dumps(rep, indent=2, default=str))
    _write(run_dir / "report.md", report.render(rep))
    return rep


def run_bench(client: MaefClient, family, seed: int, noise: int, agent_id: str, out_dir: Path, timeout: float,
              on_event=None) -> dict:
    """Submit one variant, wait for it, collect everything, grade, report. Raises only on API errors."""
    family = FAMILIES[family] if isinstance(family, str) else family
    variant = family.sample(seed, noise)
    cases = family.hidden_cases(variant)
    run = client.submit(agent_id, family.render_task(variant))
    run_id = run["run_id"]
    final = client.wait(run_id, timeout, on_event=on_event)
    return _finish(client, run_id, family, variant, cases, agent_id, {"run_id": run_id, **final},
                   Path(out_dir) / run_id)


def report_existing(client: MaefClient, run_id: str, family, seed: int, noise: int, out_dir: Path) -> dict:
    family = FAMILIES[family] if isinstance(family, str) else family
    variant = family.sample(seed, noise)
    run = client.get_run(run_id)
    return _finish(client, run_id, family, variant, family.hidden_cases(variant), run.get("agent_id", "unknown"),
                   {"run_id": run_id, **run}, Path(out_dir) / run_id)
