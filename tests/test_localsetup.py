"""`maef init` (maef_api.localsetup) and its CLI wrapper. Everything writes under tmp_path only."""
import functools
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from maef_api import localsetup
from maef_api.cli import main
from maef_api.ledger import Ledger
from maef_api.localsetup import InitError, init_local, render_profile
from maef_api.registry import ModelProfileRegistry

REPO = Path(__file__).resolve().parents[1]
PROFILE = "local-default"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    for name in ("MAEF_ARTIFACTS_URL", "MAEF_ARTIFACTS_DIR", "MAEF_DB"):
        monkeypatch.delenv(name, raising=False)
    return {
        "profiles": tmp_path / "mp",
        "artifacts": tmp_path / "artifacts",
        "db": tmp_path / "runs" / "maef.db",
    }


def init(dirs, backend="ollama", **kwargs):
    kwargs.setdefault("profiles_dir", dirs["profiles"])
    kwargs.setdefault("artifacts_dir", dirs["artifacts"])
    kwargs.setdefault("db", dirs["db"])
    return init_local(backend, **kwargs)


def load(dirs):
    profile = ModelProfileRegistry(dirs["profiles"]).get_profile(PROFILE)
    assert profile is not None
    return profile


def test_ollama_profile_written_parses_and_keeps_comments(dirs):
    report = init(dirs)

    path = dirs["profiles"] / f"{PROFILE}.yaml"
    assert report["profile"] == {"path": str(path), "action": "written", "backend": "ollama"}
    assert not any("MAEF_ARTIFACTS_URL" in w for w in report["warnings"])
    assert "# only when Ollama runs on another host" in path.read_text(encoding="utf-8")

    profile = load(dirs)
    assert profile.id == PROFILE
    assert profile.endpoint.base_url == "http://localhost:11434/v1"
    assert profile.endpoint.allow_private_network is False
    assert profile.credential_ref == "none:local"
    assert not list(dirs["profiles"].glob("*.tmp"))


def test_lmstudio_backend_uses_port_1234(dirs):
    report = init(dirs, backend="lmstudio")

    assert report["profile"]["backend"] == "lmstudio"
    profile = load(dirs)
    assert profile.endpoint.base_url == "http://localhost:1234/v1"
    assert profile.endpoint.model_id == "qwen3-8b"


@pytest.mark.parametrize(
    ("backend", "model_id", "display_name"),
    [
        ("ollama", "llama3.2:3b", "Ollama llama3.2:3b"),
        ("lmstudio", "mistral-7b", "LM Studio mistral-7b"),
    ],
)
def test_model_id_override_changes_model_and_display_name(dirs, backend, model_id, display_name):
    init(dirs, backend=backend, model_id=model_id)

    profile = load(dirs)
    assert profile.endpoint.model_id == model_id
    assert profile.display_name == display_name


def test_non_loopback_base_url_enables_private_network(dirs):
    init(dirs, base_url="http://172.20.0.1:11434/v1")

    profile = load(dirs)
    assert profile.endpoint.base_url == "http://172.20.0.1:11434/v1"
    assert profile.endpoint.allow_private_network is True
    text = (dirs["profiles"] / f"{PROFILE}.yaml").read_text(encoding="utf-8")
    assert text.count("allow_private_network: true") == 1


def test_loopback_base_url_leaves_private_network_off(dirs):
    init(dirs, base_url="http://127.0.0.1:11434/v1")

    profile = load(dirs)
    assert profile.endpoint.base_url == "http://127.0.0.1:11434/v1"
    assert profile.endpoint.allow_private_network is False


def test_second_call_keeps_existing_profile_byte_for_byte(dirs):
    init(dirs)
    path = dirs["profiles"] / f"{PROFILE}.yaml"
    before = path.read_bytes()

    report = init(dirs, backend="lmstudio", model_id="other-model", base_url="http://172.20.0.1:11434/v1")

    assert report["profile"]["action"] == "kept"
    assert path.read_bytes() == before


def test_force_rewrites_existing_profile(dirs):
    init(dirs)

    report = init(dirs, model_id="other-model", force=True)

    assert report["profile"]["action"] == "written"
    assert load(dirs).endpoint.model_id == "other-model"


def test_unknown_backend_raises_and_writes_nothing(dirs):
    with pytest.raises(InitError, match="unknown backend"):
        init(dirs, backend="nope")

    assert not dirs["profiles"].exists()
    assert not dirs["artifacts"].exists()
    assert not dirs["db"].exists()


def test_artifacts_dir_and_sqlite_ledger_created_and_idempotent(dirs):
    report = init(dirs)

    assert report["artifacts"] == str(dirs["artifacts"])
    assert dirs["artifacts"].is_dir()
    assert report["ledger"] == str(dirs["db"])
    assert dirs["db"].is_file()
    with sqlite3.connect(dirs["db"]) as conn:
        tables = conn.execute("select name from sqlite_master where type = 'table'").fetchall()
    assert tables

    again = init(dirs)

    assert again["profile"]["action"] == "kept"
    assert again["ledger"] == str(dirs["db"])
    assert dirs["db"].is_file()


def test_artifacts_dir_defaults_to_env_var(dirs, tmp_path, monkeypatch):
    env_dir = tmp_path / "env-artifacts"
    monkeypatch.setenv("MAEF_ARTIFACTS_DIR", str(env_dir))

    report = init(dirs, artifacts_dir=None)

    assert report["artifacts"] == str(env_dir)
    assert env_dir.is_dir()


def test_artifacts_url_set_warns_but_still_sets_up_locally(dirs, monkeypatch):
    monkeypatch.setenv("MAEF_ARTIFACTS_URL", "s3://bucket/prefix")

    report = init(dirs)

    assert any("MAEF_ARTIFACTS_URL" in w for w in report["warnings"])
    assert dirs["artifacts"].is_dir()


def test_postgres_db_argument_warns_and_does_not_open_ledger(dirs, monkeypatch):
    opened = []
    monkeypatch.setattr(Ledger, "open", classmethod(lambda cls, target: opened.append(target)))

    report = init(dirs, db="postgresql://x/y")

    assert any("Postgres" in w for w in report["warnings"])
    assert report["ledger"] == "postgres (not touched)"
    assert opened == []
    assert not any(dirs["profiles"].parent.rglob("*.db"))


def test_postgres_maef_db_env_warns_and_does_not_open_ledger(dirs, monkeypatch):
    monkeypatch.setenv("MAEF_DB", "postgresql://x/y")
    opened = []
    monkeypatch.setattr(Ledger, "open", classmethod(lambda cls, target: opened.append(target)))

    report = init(dirs, db=None)

    assert any("Postgres" in w for w in report["warnings"])
    assert opened == []
    assert not dirs["db"].exists()


def test_no_oci_runtime_reports_none_and_warns(dirs, monkeypatch):
    monkeypatch.setattr(localsetup, "shutil", SimpleNamespace(which=lambda name: None))

    report = init(dirs)

    assert report["oci_runtime"] is None
    assert any("podman" in w and "docker" in w for w in report["warnings"])


def test_docker_only_is_detected_without_warning(dirs, monkeypatch):
    found = {"docker": "/usr/bin/docker"}
    monkeypatch.setattr(localsetup, "shutil", SimpleNamespace(which=lambda name: found.get(name)))

    report = init(dirs)

    assert report["oci_runtime"] == "docker"
    assert not any("no podman or docker" in w for w in report["warnings"])


def _cli_with_tmp_dirs(dirs, monkeypatch):
    """`_init` calls init_local with no directory arguments, so bind tmp paths onto the real function."""
    bound = functools.partial(
        localsetup.init_local,
        profiles_dir=dirs["profiles"],
        artifacts_dir=dirs["artifacts"],
        db=dirs["db"],
    )
    monkeypatch.setattr(localsetup, "init_local", bound)


def test_cli_init_json_prints_report(dirs, monkeypatch, capsys):
    _cli_with_tmp_dirs(dirs, monkeypatch)

    assert main(["init", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert {"profile", "artifacts", "ledger"} <= report.keys()
    assert report["profile"]["action"] == "written"
    assert (dirs["profiles"] / f"{PROFILE}.yaml").is_file()


def test_cli_init_unknown_backend_returns_2(dirs, monkeypatch, capsys):
    _cli_with_tmp_dirs(dirs, monkeypatch)

    assert main(["init", "--backend", "nope"]) == 2

    assert not dirs["profiles"].exists()
    assert not dirs["db"].exists()


def test_local_first_import_works_without_boto3(tmp_path):
    code = textwrap.dedent(
        """
        import sys
        sys.modules["boto3"] = None
        sys.modules["botocore"] = None
        import maef_api.artifacts as artifacts

        store = artifacts.default_artifact_store()
        assert type(store).__name__ == "LocalArtifactStore", type(store).__name__

        import maef_api.worker
        import maef_api.localsetup
        import maef_api.ledger
        print("ok")
        """
    )
    env = {k: v for k, v in os.environ.items() if k != "MAEF_ARTIFACTS_URL"}
    env.update(
        MAEF_DB=str(tmp_path / "maef.db"),
        MAEF_ARTIFACTS_DIR=str(tmp_path / "artifacts"),
        PYTHONPATH=str(REPO / "apps" / "api"),
    )

    result = subprocess.run(
        [sys.executable, "-c", code], check=False, cwd=REPO, env=env, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_openrouter_backend_writes_https_profile_with_env_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MAEF_ARTIFACTS_URL", raising=False)
    report = init_local("openrouter", "qwen/qwen3.7-flash", profiles_dir=tmp_path / "mp",
                        artifacts_dir=tmp_path / "a", db=tmp_path / "m.db")
    profile = ModelProfileRegistry(tmp_path / "mp").get_profile("local-default")
    assert profile.endpoint.base_url == "https://openrouter.ai/api/v1"
    assert profile.endpoint.model_id == "qwen/qwen3.7-flash"
    assert profile.credential_ref == "env:OPENROUTER_API_KEY"
    assert profile.endpoint.allow_private_network is False
    assert profile.display_name == "OpenRouter qwen/qwen3.7-flash"
    assert any("OPENROUTER_API_KEY" in w and "leave this machine" in w for w in report["warnings"])


def test_openrouter_default_model_is_a_free_one(tmp_path):
    text = render_profile("openrouter")
    assert "model_id: nvidia/nemotron-3.5-lightning:free" in text
    assert "id: local-default" in text


def test_local_backends_do_not_warn_about_hosted_services(tmp_path, monkeypatch):
    monkeypatch.delenv("MAEF_ARTIFACTS_URL", raising=False)
    report = init_local("ollama", profiles_dir=tmp_path / "mp", artifacts_dir=tmp_path / "a", db=tmp_path / "m.db")
    assert not any("hosted service" in w for w in report["warnings"])
