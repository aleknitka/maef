"""`maef init`: set up a fully local install (no cloud account needed).

Writes `model-profiles/local-default.yaml` (the profile the shipped agents use) from the Ollama or LM Studio
example, creates the local artifact directory and the SQLite ledger, and reports anything still pointing at a
remote service. Idempotent: an existing profile is kept unless `force`."""

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .models import ModelEndpointProfile
from .project import MARKER, bundled_dir, project_root, source_checkout

PROFILE_ID = "local-default"
BACKENDS = {"ollama": "ollama.yaml", "lmstudio": "lmstudio.yaml", "openrouter": "openrouter.yaml"}
VENDORS = {"ollama": "Ollama", "lmstudio": "LM Studio", "openrouter": "OpenRouter"}
REMOTE_BACKENDS = {"openrouter": "OPENROUTER_API_KEY"}
STARTER_DIRS = ("agents", "sandbox-profiles", "mcp-servers", "examples/model-profiles")
MARKER_TEXT = """# Marks this folder as a MAEF project: `maef` and the API find it from any subfolder.
# Nothing needs to be configured here; settings come from MAEF_* environment variables.
"""
GITIGNORE_TEXT = """# Local state and machine-specific model endpoints, never committed
runs/
artifacts/
/model-profiles/
*.db
"""


class InitError(Exception):
    pass


def _examples_dir(root: Path) -> Path:
    own = root / "examples" / "model-profiles"
    return own if own.is_dir() else bundled_dir() / "examples" / "model-profiles"


def scaffold_target() -> Path | None:
    """The folder `maef init` must fill with starter files, or None when a project already exists
    (a maef.toml above the current folder, or a source checkout)."""
    home = os.environ.get("MAEF_HOME")
    if home:
        target = Path(home).expanduser().resolve()
        return None if (target / MARKER).is_file() or target == source_checkout() else target
    cwd = Path.cwd().resolve()
    if any((p / MARKER).is_file() for p in (cwd, *cwd.parents)) or source_checkout() is not None:
        return None
    return cwd


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603, S607  argv list, no shell; fixed 'git' executable and fixed arguments


def _in_git_repo(path: Path) -> bool:
    return _git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def scaffold_project(target: Path) -> dict:
    """Copy the bundled starter files into `target`, write maef.toml and .gitignore (existing files are kept),
    and make the starter files a git commit unless `target` is already inside a repository.

    Agent definitions are pinned by their git revision, and a run is refused when the agent directory has
    uncommitted changes, so the starter files must be committed for the first run to work."""
    if shutil.which("git") is None:
        raise InitError(
            "git is required: agent definitions are pinned by git revision. Install git, then run `maef init` again."
        )
    target.mkdir(parents=True, exist_ok=True)
    source = bundled_dir()
    report: dict = {"root": str(target), "copied": [], "git": None}
    for rel in STARTER_DIRS:
        if not (source / rel).is_dir():
            raise InitError(f"starter files missing from this installation: {rel}")
        for src in sorted(p for p in (source / rel).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            dest = target / src.relative_to(source)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            report["copied"].append(dest.relative_to(target).as_posix())
    for name, text in ((MARKER, MARKER_TEXT), (".gitignore", GITIGNORE_TEXT)):
        if not (target / name).exists():
            (target / name).write_text(text, encoding="utf-8")
            report["copied"].append(name)
    if _in_git_repo(target):
        report["git"] = "existing"
        return report
    for step in (["init", "--quiet"], ["add", "--", MARKER, ".gitignore", *STARTER_DIRS]):
        done = _git(target, *step)
        if done.returncode != 0:
            raise InitError(f"git {step[0]} failed: {(done.stderr or done.stdout).strip()}")
    identity = (
        []
        if _git(target, "config", "user.name").stdout.strip()
        else ["-c", "user.name=maef", "-c", "user.email=maef@localhost"]
    )
    done = _git(
        target, *identity, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Starter files from maef init"
    )
    if done.returncode != 0:
        raise InitError(f"git commit failed: {(done.stderr or done.stdout).strip()}")
    report["git"] = "initialised"
    return report


def _is_loopback(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _set_line(text: str, key: str, value: str, indent: str = "") -> str:
    pattern = re.compile(rf"^{indent}{key}:.*$", re.MULTILINE)
    if not pattern.search(text):
        raise InitError(f"example profile has no {key!r} line")
    return pattern.sub(f"{indent}{key}: {value}", text, count=1)


def render_profile(
    backend: str, model_id: str | None = None, base_url: str | None = None, examples_dir: Path | None = None
) -> str:
    """The example profile text with id `local-default` and optional overrides (comments kept)."""
    if backend not in BACKENDS:
        raise InitError(f"unknown backend {backend!r}; choose one of {', '.join(BACKENDS)}")
    examples_dir = examples_dir if examples_dir is not None else _examples_dir(project_root())
    text = (examples_dir / BACKENDS[backend]).read_text(encoding="utf-8")
    text = _set_line(text, "id", PROFILE_ID)
    if model_id:
        text = _set_line(text, "model_id", model_id, "  ")
        text = _set_line(text, "display_name", f"{VENDORS[backend]} {model_id}")
    if base_url:
        text = _set_line(text, "base_url", base_url, "  ")
        if not _is_loopback(base_url):
            # e.g. WSL2 reaching Ollama on the Windows host: a private address must be opted into.
            text = re.sub(
                r"^  # allow_private_network:.*$", "  allow_private_network: true", text, count=1, flags=re.MULTILINE
            )
            if "allow_private_network: true" not in text:
                text = _set_line(text, "base_url", f"{base_url}\n  allow_private_network: true", "  ")
    profile = ModelEndpointProfile.model_validate(yaml.safe_load(text))  # fail before writing anything
    if profile.id != PROFILE_ID:
        raise InitError("rendered profile has the wrong id")
    return text


def init_local(
    backend: str = "ollama",
    model_id: str | None = None,
    base_url: str | None = None,
    force: bool = False,
    profiles_dir: Path | None = None,
    artifacts_dir: Path | None = None,
    db: str | Path | None = None,
    examples_dir: Path | None = None,
    root: Path | None = None,
) -> dict:
    root = Path(root) if root is not None else project_root()
    examples_dir = examples_dir if examples_dir is not None else _examples_dir(root)
    report: dict = {"warnings": []}
    profiles_dir = Path(profiles_dir) if profiles_dir is not None else root / "model-profiles"
    path = profiles_dir / f"{PROFILE_ID}.yaml"
    if path.exists() and not force:
        report["profile"] = {"path": str(path), "action": "kept"}
    else:
        text = render_profile(backend, model_id, base_url, examples_dir)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        report["profile"] = {"path": str(path), "action": "written", "backend": backend}
        if backend in REMOTE_BACKENDS:
            key = REMOTE_BACKENDS[backend]
            report["warnings"].append(
                f"{VENDORS[backend]} is a hosted service: prompts and tool results leave this "
                f"machine. Export {key} in the worker's environment."
            )

    if os.environ.get("MAEF_ARTIFACTS_URL"):
        report["warnings"].append(
            "MAEF_ARTIFACTS_URL is set: artifacts go to S3, not this machine. "
            "Unset it to use the local directory store."
        )
    artifacts = Path(artifacts_dir or os.environ.get("MAEF_ARTIFACTS_DIR") or root / "artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    report["artifacts"] = str(artifacts)

    db = str(db or os.environ.get("MAEF_DB") or root / "runs" / "maef.db")
    if db.startswith(("postgres://", "postgresql://")):
        report["warnings"].append("MAEF_DB points at Postgres; unset it to use the local SQLite ledger.")
        report["ledger"] = "postgres (not touched)"
    else:
        from .ledger import Ledger

        Path(db).parent.mkdir(parents=True, exist_ok=True)
        Ledger.open(db)  # creates and migrates the SQLite file
        report["ledger"] = db

    runtimes = [r for r in ("podman", "docker") if shutil.which(r)]
    report["oci_runtime"] = runtimes[0] if runtimes else None
    if not runtimes:
        report["warnings"].append(
            "no podman or docker found: agents with run_command or MCP servers cannot run "
            "(tool-less agents still work)."
        )
    return report
