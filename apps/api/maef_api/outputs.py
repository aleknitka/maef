"""Collect a run's files from the sandbox: regular files directly under /workspace/out/."""

import mimetypes
from dataclasses import dataclass, field

from .artifacts import (
    MAX_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACTS,
    SAFE_MIMES,
    clean_name,
)
from .sandbox import WORKSPACE, Session

OUT_DIR = f"{WORKSPACE}/out"
LIST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class OutputFile:
    name: str
    mime: str
    data: bytes


@dataclass
class Collected:
    files: list[OutputFile] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {"name", "reason"}; names are sanitised


def guess_mime(name: str) -> str:
    guessed = mimetypes.guess_type(name, strict=False)[0]
    return guessed if guessed in SAFE_MIMES else "application/octet-stream"


def collect_outputs(
    session: Session, *, budget_files: int = MAX_RUN_ARTIFACTS - 1, budget_bytes: int = MAX_RUN_ARTIFACT_BYTES
) -> Collected:
    """List with busybox-safe `find` (-type f never matches symlinks, no recursion), then read each
    file. Raises SandboxError if the listing fails; per-file problems go to `skipped`."""
    from .sandbox import SandboxError

    done = session.exec(["find", OUT_DIR, "-maxdepth", "1", "-type", "f", "-print0"], timeout=LIST_TIMEOUT_SECONDS)
    if done.exit_code != 0 or done.timed_out:
        # No /workspace/out at all is the normal case for runs that wrote nothing.
        return Collected()
    result = Collected()
    total = 0
    for raw in sorted(p for p in done.stdout.split(b"\0") if p):
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            result.skipped.append({"name": "?", "reason": "bad_name"})
            continue
        base = path.rsplit("/", 1)[-1]
        name = clean_name(base)
        # Only direct children of OUT_DIR, even if the listing command misbehaves.
        if path.rsplit("/", 1)[0] != OUT_DIR or not base or any(ord(c) < 32 for c in base):
            result.skipped.append({"name": name, "reason": "bad_name"})
            continue
        if len(result.files) >= budget_files:
            result.skipped.append({"name": name, "reason": "too_many_files"})
            continue
        try:
            data = session.get(path)
        except SandboxError as exc:
            result.skipped.append({"name": name, "reason": str(exc)})
            continue
        if len(data) > MAX_ARTIFACT_BYTES:
            result.skipped.append({"name": name, "reason": "too_large"})
        elif total + len(data) > budget_bytes:
            result.skipped.append({"name": name, "reason": "run_limit"})
        else:
            total += len(data)
            result.files.append(OutputFile(name, guess_mime(name), data))
    return result
