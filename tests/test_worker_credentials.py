import logging
import shutil
from pathlib import Path

import pytest

from maef_api.credentials import CredentialNotAllowed, Credentials, referenced_env_names, worker_allowed_credentials
from maef_api.lint import lint_repo
from maef_api.registry import ROOT

SECRET = "s3cr3t-value-do-not-leak"


def project(tmp_path: Path, *, profile_ref="env:OPENROUTER_API_KEY", secret_ref=None) -> Path:
    (tmp_path / "model-profiles").mkdir()
    (tmp_path / "mcp-servers").mkdir()
    (tmp_path / "model-profiles" / "p.yaml").write_text(f"id: p\ncredential_ref: {profile_ref}\n")
    refs = f"secrets:\n  KEY: {secret_ref}\n" if secret_ref else "secrets: {}\n"
    (tmp_path / "mcp-servers" / "m.yaml").write_text("id: m\n" + refs)
    return tmp_path


def creds(root):
    environ = {"OPENROUTER_API_KEY": SECRET, "MAEF_WORKER_CREDENTIAL": SECRET, "TOOL_TOKEN": SECRET}
    return Credentials(environ, allowed=lambda: worker_allowed_credentials(root, environ))


def test_names_come_from_profiles_and_manifests(tmp_path):
    root = project(tmp_path, secret_ref="env:TOOL_TOKEN")
    assert referenced_env_names(root) == {"OPENROUTER_API_KEY", "TOOL_TOKEN"}
    c = creds(root)
    assert c.resolve("env:OPENROUTER_API_KEY").reveal() == SECRET
    assert c.resolve("env:TOOL_TOKEN").reveal() == SECRET
    assert c.resolve("none:local") is None and c.resolve("aws:default-chain") is None


def test_unreferenced_name_is_refused_without_leaking_value(tmp_path, caplog):
    c = creds(project(tmp_path))
    with caplog.at_level(logging.INFO), pytest.raises(CredentialNotAllowed) as exc:
        c.resolve("env:MAEF_WORKER_CREDENTIAL")
    assert "MAEF_WORKER_CREDENTIAL" in str(exc.value)
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert "MAEF_WORKER_CREDENTIAL" in caplog.text


def test_not_allowed_without_a_project_folder():
    c = Credentials({"X_KEY": SECRET}, allowed=lambda: worker_allowed_credentials(None, {}))
    with pytest.raises(CredentialNotAllowed):
        c.resolve("env:X_KEY")


def test_env_var_names_and_globs_allow(tmp_path):
    root = project(tmp_path)
    environ = {"MAEF_WORKER_ALLOWED_CREDENTIALS": "EXACT_ONE, MYCO_*", "EXACT_ONE": "1", "MYCO_A": "2", "OTHER": "3"}
    c = Credentials(environ, allowed=lambda: worker_allowed_credentials(root, environ))
    assert c.resolve("env:EXACT_ONE").reveal() == "1"
    assert c.resolve("env:MYCO_A").reveal() == "2"
    with pytest.raises(CredentialNotAllowed):
        c.resolve("env:OTHER")


def test_default_credentials_stay_unrestricted():
    assert Credentials({"ANY_THING": "v"}).resolve("env:ANY_THING").reveal() == "v"


def test_worker_factory_refuses_before_any_call(tmp_path):
    from maef_api.worker import default_adapter_factory
    from test_http_adapters import make_profile

    factory = default_adapter_factory(creds(project(tmp_path)))
    profile = make_profile("openai-compatible", "https://example.com/v1", cred="env:MAEF_WORKER_CREDENTIAL")
    with pytest.raises(CredentialNotAllowed):
        factory(profile)


def lint_copy(tmp_path, hosts):
    for d in ("agents", "sandbox-profiles", "mcp-servers"):
        shutil.copytree(ROOT / d, tmp_path / d)
    f = tmp_path / "mcp-servers" / "fetch.yaml"
    text = f.read_text()
    start = text.index("allowed_hosts:")
    end = text.index("allowed_ports:")
    f.write_text(text[:start] + f"allowed_hosts: {hosts}\n" + text[end:])
    return lint_repo(tmp_path)


def test_lint_warns_on_star_allowlist(tmp_path):
    findings = lint_copy(tmp_path, '["*"]')
    assert any(f.severity == "warning" and f.path == "mcp-servers/fetch.yaml" and '"*"' in f.message for f in findings)


def test_shipped_fetch_manifest_has_explicit_hosts():
    assert lint_repo(ROOT) == []
    hosts = (ROOT / "mcp-servers" / "fetch.yaml").read_text().split("allowed_hosts:")[1].split("allowed_ports")[0]
    assert "*" not in hosts.replace("#", "")
