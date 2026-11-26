"""Project folder resolution, `maef init` in an empty folder, the version flag and the CLI exit codes."""

import os
import subprocess
import sys
import textwrap
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from maef_api import localsetup, project
from maef_api.cli import main
from maef_api.definitions import Definitions
from maef_api.registry import AgentRegistry, McpServerRegistry, ModelProfileRegistry, SandboxProfileRegistry

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def not_a_checkout(tmp_path, monkeypatch):
    """cwd is an empty folder and the package does not look like it runs from a source checkout."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(project, "_CHECKOUT", tmp_path / "no-checkout")
    monkeypatch.setattr(localsetup, "source_checkout", lambda: None)
    monkeypatch.setattr(localsetup, "bundled_dir", lambda: REPO)  # a wheel would serve its _bundled copy
    for name in ("MAEF_HOME", "MAEF_ARTIFACTS_URL", "MAEF_ARTIFACTS_DIR", "MAEF_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)  # no identity: init must cope
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return work


def test_project_root_prefers_maef_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAEF_HOME", str(tmp_path))
    assert project.project_root() == tmp_path.resolve()


def test_project_root_finds_the_nearest_maef_toml_above_cwd(not_a_checkout):
    (not_a_checkout / "maef.toml").write_text("", encoding="utf-8")
    deep = not_a_checkout / "a" / "b"
    deep.mkdir(parents=True)
    os.chdir(deep)
    assert project.project_root() == not_a_checkout.resolve()


def test_project_root_falls_back_to_the_source_checkout():
    assert project.project_root() == REPO
    assert project.bundled_dir() == REPO  # no wheel here: the repository folders are the starter files


def test_project_root_errors_with_a_pointer_to_init(not_a_checkout):
    with pytest.raises(project.ProjectNotFoundError, match="maef init"):
        project.project_root()


def test_importing_the_package_never_resolves_the_project(tmp_path):
    code = textwrap.dedent(
        """
        import maef_api.project as p

        def boom():
            raise AssertionError("project resolved at import")

        p.project_root = boom
        import maef_api, maef_api.registry, maef_api.schemas, maef_api.localsetup, maef_api.cli
        import maef_api.auth, maef_api.artifacts, maef_api.bench.grade, maef_api.bench.report
        print("ok")
        """
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "apps" / "api")}
    done = subprocess.run(
        [sys.executable, "-c", code], check=False, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_version_flag_prints_the_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"maef {metadata.version('maef-control-plane')}"


def test_init_in_an_empty_folder_scaffolds_commits_and_pins(not_a_checkout, capsys):
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "starter files" in out and "committed" in out
    for rel in (
        "maef.toml",
        ".gitignore",
        "agents/research-analyst/agent.yaml",
        "sandbox-profiles/python-sandbox.yaml",
    ):
        assert (not_a_checkout / rel).is_file(), rel
    assert (not_a_checkout / "model-profiles" / "local-default.yaml").is_file()
    assert (not_a_checkout / "runs" / "maef.db").is_file()
    log = subprocess.run(
        ["git", "-C", str(not_a_checkout), "log", "--oneline"], capture_output=True, text=True, check=True
    )
    assert len(log.stdout.splitlines()) == 1
    assert project.project_root() == not_a_checkout.resolve()

    defs = Definitions(
        AgentRegistry(not_a_checkout / "agents"),
        ModelProfileRegistry(not_a_checkout / "model-profiles"),
        SandboxProfileRegistry(not_a_checkout / "sandbox-profiles"),
        McpServerRegistry(not_a_checkout / "mcp-servers"),
    )
    pinned = defs.resolve("research-analyst")
    assert pinned.git_rev and pinned.dirty is False

    assert main(["init"]) == 0  # idempotent: the second run sees maef.toml and keeps everything
    assert main(["lint"]) == 0


def test_init_inside_an_existing_repository_leaves_the_commit_to_the_user(not_a_checkout, capsys):
    subprocess.run(["git", "init", "--quiet", str(not_a_checkout)], check=True)
    assert main(["init"]) == 0
    assert "commit the starter files" in capsys.readouterr().out
    log = subprocess.run(
        ["git", "-C", str(not_a_checkout), "log", "--oneline"], capture_output=True, text=True, check=False
    )
    assert log.stdout.strip() == ""


def test_init_without_git_fails_clearly_and_writes_nothing(not_a_checkout, monkeypatch, capsys):
    monkeypatch.setattr(localsetup, "shutil", SimpleNamespace(which=lambda name: None))
    assert main(["init"]) == 1
    assert "git is required" in capsys.readouterr().err
    assert list(not_a_checkout.iterdir()) == []


def test_init_in_a_read_only_folder_exits_1(not_a_checkout, capsys):
    not_a_checkout.chmod(0o500)
    try:
        if os.access(not_a_checkout, os.W_OK):
            pytest.skip("directory permissions are not enforced (running as root)")
        assert main(["init"]) == 1
        assert capsys.readouterr().err.startswith("error:")
    finally:
        not_a_checkout.chmod(0o700)


def test_init_with_an_invalid_base_url_exits_1(not_a_checkout, capsys):
    assert main(["init", "--base-url", "not a url"]) == 1
    assert "error:" in capsys.readouterr().err


def test_init_with_an_unwritable_artifacts_dir_exits_1(not_a_checkout, monkeypatch, capsys):
    blocker = not_a_checkout / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    monkeypatch.setenv("MAEF_ARTIFACTS_DIR", str(blocker / "artifacts"))
    assert main(["init"]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAEF_DB", str(tmp_path / "maef.db"))
    monkeypatch.setenv("MAEF_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    return tmp_path


def test_failures_exit_1_with_a_message_on_stderr(local_state, capsys):
    cases = [
        ["lint", str(local_state / "nothing-here")],
        ["schema", "export", "--check", "--dir", str(local_state)],
        ["admin", "token", "revoke", "tok-missing"],
        ["admin", "worker", "revoke", "no-such-worker"],
        ["eval", "run", "no-such-suite"],
    ]
    for argv in cases:
        assert main(argv) == 1, argv
        assert capsys.readouterr().err.strip(), argv


def test_unreachable_api_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("MAEF_API_URL", "http://127.0.0.1:1")
    assert main(["agents"]) == 1
    assert "error:" in capsys.readouterr().err


def test_usage_errors_exit_2(capsys):
    for argv in (["frobnicate"], ["run"], ["init", "--backend", "nope"], ["otel", "export"]):
        assert main(argv) == 2, argv
        assert capsys.readouterr().err.strip(), argv


def test_a_failing_command_exits_non_zero_as_a_process(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO / "apps" / "api"), "MAEF_DB": str(tmp_path / "m.db")}
    done = subprocess.run(
        [sys.executable, "-m", "maef_api.cli", "admin", "token", "revoke", "tok-missing"],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 1 and "no active token" in done.stderr
