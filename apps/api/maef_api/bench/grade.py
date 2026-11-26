"""Run a family's hidden-case grader against the collected deliverables in a locked-down, network-less container."""

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import yaml

from ..registry import ROOT
from ..sandbox import detect_sandbox

TIMEOUT_SECONDS = 120
PROFILE = ROOT / "sandbox-profiles" / "python-sandbox.yaml"
NOT_DELIVERABLES = {"output.md", "transcript.jsonl"}


def _error(msg: str) -> dict:
    return {"passed": 0, "failed": 0, "total": 0, "errors": [], "error": msg}


def _image() -> str:
    return yaml.safe_load(PROFILE.read_text(encoding="utf-8"))["image"]


def _mount_opts() -> str:
    # SELinux hosts need a relabel so the container may read the mounts; other hosts ignore the flag's absence.
    return "ro,Z" if Path("/sys/fs/selinux").exists() else "ro"


def stage_deliverables(artifacts_dir: Path, dest: Path) -> list[str]:
    """Copy agent files (flattened by basename, skipping run metadata) into `dest`; return their names."""
    names = []
    for f in sorted(artifacts_dir.rglob("*")):
        if f.is_file() and f.name not in NOT_DELIVERABLES:
            shutil.copyfile(f, dest / f.name)
            os.chmod(dest / f.name, 0o644)
            names.append(f.name)
    return names


def run_grader(grader_source: str, cases: list[dict], artifacts_dir: Path) -> dict:
    """-> {"passed","failed","total","errors":[...]} or the same shape with an "error" string. Never raises."""
    try:
        sandbox = detect_sandbox()
        binary = shutil.which(sandbox.runtime) if sandbox else None
        if not sandbox or not binary:
            return _error("no podman or docker available")
        runtime, image = sandbox.runtime, _image()
        with tempfile.TemporaryDirectory(prefix="maef-grade-") as tmp:
            work, grader = Path(tmp) / "work", Path(tmp) / "grader"
            for d in (Path(tmp), work, grader):
                d.mkdir(exist_ok=True)
                os.chmod(d, 0o755)
            stage_deliverables(artifacts_dir, work)
            (grader / "grade.py").write_text(grader_source, encoding="utf-8")
            (grader / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
            for f in grader.iterdir():
                os.chmod(f, 0o644)
            name = f"maef-grade-{uuid.uuid4().hex[:12]}"
            opts = _mount_opts()
            args = [
                binary,
                "run",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "all",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--memory",
                "256m",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/tmp",
                "-v",
                f"{work}:/work:{opts}",
                "-v",
                f"{grader}:/grader:{opts}",
                image,
                "python3",
                "-I",
                "/grader/grade.py",
            ]
            try:
                done = subprocess.run(
                    args, capture_output=True, timeout=TIMEOUT_SECONDS, check=False, stdin=subprocess.DEVNULL
                )
            except subprocess.TimeoutExpired:
                _rm(binary, runtime, name)
                return _error(f"grader timed out after {TIMEOUT_SECONDS}s")
    except Exception as exc:  # noqa: BLE001  # the contract is to report, not raise
        return _error(f"grading failed: {type(exc).__name__}: {exc}"[:300])
    return parse_output(
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
        done.returncode,
        len(cases),
    )


def parse_output(stdout: str, stderr: str, returncode: int, total: int) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "passed" in data and "failed" in data:
            return {
                "passed": data["passed"],
                "failed": data["failed"],
                "total": total,
                "errors": list(data.get("errors") or [])[:20],
            }
    return _error(f"no result line from the grader (exit {returncode}): {stderr.strip()[-300:]}")


def _rm(binary: str, runtime: str, name: str) -> None:
    try:
        subprocess.run(
            [binary, "rm", "-f", "-t", "0", name] if runtime == "podman" else [binary, "rm", "-f", name],
            capture_output=True,
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass
