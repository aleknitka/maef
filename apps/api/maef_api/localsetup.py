"""`maef init`: set up a fully local install (no cloud account needed).

Writes `model-profiles/local-default.yaml` (the profile the shipped agents use) from the Ollama or LM Studio
example, creates the local artifact directory and the SQLite ledger, and reports anything still pointing at a
remote service. Idempotent: an existing profile is kept unless `force`."""
import ipaddress
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .models import ModelEndpointProfile
from .registry import MODEL_PROFILES_DIR, ROOT

PROFILE_ID = "local-default"
BACKENDS = {"ollama": "ollama.yaml", "lmstudio": "lmstudio.yaml"}
EXAMPLES_DIR = ROOT / "examples" / "model-profiles"


class InitError(Exception):
    pass


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


def render_profile(backend: str, model_id: str | None = None, base_url: str | None = None,
                   examples_dir: Path = EXAMPLES_DIR) -> str:
    """The example profile text with id `local-default` and optional overrides (comments kept)."""
    if backend not in BACKENDS:
        raise InitError(f"unknown backend {backend!r}; choose one of {', '.join(BACKENDS)}")
    text = (examples_dir / BACKENDS[backend]).read_text(encoding="utf-8")
    text = _set_line(text, "id", PROFILE_ID)
    if model_id:
        text = _set_line(text, "model_id", model_id, "  ")
        text = _set_line(text, "display_name", f"{'Ollama' if backend == 'ollama' else 'LM Studio'} {model_id}")
    if base_url:
        text = _set_line(text, "base_url", base_url, "  ")
        if not _is_loopback(base_url):
            # e.g. WSL2 reaching Ollama on the Windows host: a private address must be opted into.
            text = re.sub(r"^  # allow_private_network:.*$", "  allow_private_network: true", text, count=1, flags=re.MULTILINE)
            if "allow_private_network: true" not in text:
                text = _set_line(text, "base_url", f"{base_url}\n  allow_private_network: true", "  ")
    profile = ModelEndpointProfile.model_validate(yaml.safe_load(text))  # fail before writing anything
    if profile.id != PROFILE_ID:
        raise InitError("rendered profile has the wrong id")
    return text


def init_local(backend: str = "ollama", model_id: str | None = None, base_url: str | None = None,
               force: bool = False, profiles_dir: Path = MODEL_PROFILES_DIR,
               artifacts_dir: Path | None = None, db: str | Path | None = None,
               examples_dir: Path = EXAMPLES_DIR) -> dict:
    report: dict = {"warnings": []}
    profiles_dir = Path(profiles_dir)
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

    if os.environ.get("MAEF_ARTIFACTS_URL"):
        report["warnings"].append("MAEF_ARTIFACTS_URL is set: artifacts go to S3, not this machine. "
                                  "Unset it to use the local directory store.")
    artifacts = Path(artifacts_dir or os.environ.get("MAEF_ARTIFACTS_DIR") or ROOT / "artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    report["artifacts"] = str(artifacts)

    db = str(db or os.environ.get("MAEF_DB") or ROOT / "runs" / "maef.db")
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
        report["warnings"].append("no podman or docker found: agents with run_command or MCP servers cannot run "
                                  "(tool-less agents still work).")
    return report
