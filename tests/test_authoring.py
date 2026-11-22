import json
import os
import re
import shutil

import pytest
from conftest import copy_research_analyst
from fastapi.testclient import TestClient
from maef_api import main
from maef_api.cli import main as cli_main
from maef_api.client import AsgiTransport, MaefClient
from maef_api.definitions import Definitions, ResolveError
from maef_api.drafts import MAX_FILE_BYTES, DraftRejected, Drafts
from maef_api.gitrepo import GitError, GitRepo, run_git
from maef_api.ledger import Ledger
from maef_api.lint import lint_repo
from maef_api.registry import (
    ROOT,
    AgentRegistry,
    McpServerRegistry,
    ModelProfileRegistry,
    SandboxProfileRegistry,
)
from maef_api.runs import Runs
from maef_api.schemas import check_schemas, schema_for, write_schemas
from test_api import PROFILE

AGENT = "research-analyst"
AGENT_DIR = f"agents/{AGENT}"
AGENT_FILE = f"{AGENT_DIR}/agent.yaml"
SYSTEM = f"{AGENT_DIR}/SYSTEM.md"


def git(cwd, *args):
    return run_git(cwd, *args).decode().strip()


def head(repo):
    return git(repo, "rev-parse", "HEAD")


def commit_all(repo, message="commit"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


def rewrite(path, fn):
    path.write_text(fn(path.read_text()))


def has_error(findings, path, text):
    return any(f.severity == "error" and f.path == path and text in f.message for f in findings)


@pytest.fixture
def repo(tmp_path):
    """A committed git repo: agents/research-analyst (no MCP, no network), mcp-servers/, sandbox-profiles/."""
    root = tmp_path / "repo"
    (root / "agents").mkdir(parents=True)
    copy_research_analyst(root / "agents")
    for name in ("mcp-servers", "sandbox-profiles"):
        shutil.copytree(ROOT / name, root / name)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test Author")
    git(root, "config", "user.email", "author@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    commit_all(root, "init")
    return root


@pytest.fixture
def profiles(tmp_path):
    path = tmp_path / "profiles"
    path.mkdir()
    (path / "local-default.yaml").write_text(PROFILE.format(id="local-default", extra=""))
    return path


def definitions(repo, profiles):
    return Definitions(AgentRegistry(repo / "agents"), ModelProfileRegistry(profiles),
                       SandboxProfileRegistry(repo / "sandbox-profiles"), McpServerRegistry(repo / "mcp-servers"),
                       repo_root=repo)


@pytest.fixture
def store(repo):
    return Drafts(repo)


def tree(repo, draft):
    return repo / "runs" / "drafts" / draft.id


def branch_exists(repo, branch):
    try:
        git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except GitError:
        return False
    return True


# -- gitrepo --

def test_resolve_revision_accepts_sha_head_branch_and_tag(repo):
    sha = head(repo)
    git(repo, "branch", "feature-x")
    git(repo, "tag", "v1")
    gr = GitRepo(repo)
    assert gr.resolve_revision(sha) == sha
    assert gr.resolve_revision("HEAD") == sha
    assert gr.resolve_revision("feature-x") == sha
    assert gr.resolve_revision("v1") == sha


@pytest.mark.parametrize("ref", ["HEAD~1", "HEAD^", "-x", "--help", "no-such-ref", "0" * 40])
def test_resolve_revision_refuses_expressions_options_and_unknown(repo, ref):
    (repo / "second.txt").write_text("2\n")
    commit_all(repo, "second")  # HEAD~1 exists, so the refusal comes from the grammar, not the repo
    with pytest.raises(GitError):
        GitRepo(repo).resolve_revision(ref)


def test_read_blob_refuses_unsafe_paths(repo):
    sha = head(repo)
    gr = GitRepo(repo)
    for bad in ["../x", f"{AGENT_DIR}/../SYSTEM.md", "/etc/passwd", "", "agents//x"]:
        with pytest.raises(GitError):
            gr.read_blob(sha, bad)


def test_read_blob_refuses_symlinks_and_directories(repo):
    os.symlink("SYSTEM.md", repo / AGENT_DIR / "link.md")
    sha = commit_all(repo, "symlink")
    gr = GitRepo(repo)
    with pytest.raises(GitError, match="not a regular file"):
        gr.read_blob(sha, f"{AGENT_DIR}/link.md")
    with pytest.raises(GitError, match="not a regular file"):
        gr.read_blob(sha, AGENT_DIR)
    assert gr.read_blob(sha, SYSTEM) is not None
    assert gr.read_blob(sha, f"{AGENT_DIR}/missing.md") is None


# -- definitions pinned to a revision --

def test_resolve_revision_pins_commit_as_clean(repo, profiles):
    sha = head(repo)
    pinned = definitions(repo, profiles).resolve(AGENT, sha)
    assert pinned.git_rev == sha
    assert pinned.dirty is False


def test_clean_revision_digest_equals_working_tree_digest(repo, profiles):
    defs = definitions(repo, profiles)
    working = defs.resolve(AGENT)
    assert working.dirty is False
    assert defs.resolve(AGENT, head(repo)).definition_digest == working.definition_digest


def test_revision_ignores_uncommitted_edits(repo, profiles):
    defs = definitions(repo, profiles)
    committed = (repo / SYSTEM).read_text()
    sha = head(repo)
    (repo / SYSTEM).write_text("edited, not committed")
    assert defs.resolve(AGENT, sha).instructions == committed
    working = defs.resolve(AGENT)
    assert working.instructions == "edited, not committed"
    assert working.dirty is True


def test_unknown_revision_is_resolve_error(repo, profiles):
    defs = definitions(repo, profiles)
    with pytest.raises(ResolveError):
        defs.resolve(AGENT, "0" * 40)
    with pytest.raises(ResolveError):
        defs.resolve(AGENT, "no-such-branch")


# -- schemas --

def test_schema_write_then_check_has_no_drift(tmp_path):
    write_schemas(tmp_path)
    assert check_schemas(tmp_path) == []


def test_schema_change_is_drift(tmp_path):
    write_schemas(tmp_path)
    (tmp_path / "agent-definition.schema.json").write_text("{}\n")
    assert check_schemas(tmp_path) == ["agent-definition"]


def test_cli_schema_export_check_exit_codes(tmp_path):
    d = str(tmp_path / "schemas")
    assert cli_main(["schema", "export", "--check", "--dir", d]) == 1  # nothing written yet
    assert cli_main(["schema", "export", "--dir", d]) == 0
    assert cli_main(["schema", "export", "--check", "--dir", d]) == 0
    (tmp_path / "schemas" / "sandbox-profile.schema.json").write_text("{}\n")
    assert cli_main(["schema", "export", "--check", "--dir", d]) == 1


def test_committed_schemas_in_repo_have_no_drift():
    assert check_schemas() == []
    assert cli_main(["schema", "export", "--check"]) == 0


# -- lint --

def test_lint_clean_repo_has_no_findings(repo):
    assert lint_repo(repo) == []


def test_lint_unknown_tool(repo):
    rewrite(repo / AGENT_FILE, lambda t: t + "tools: [bogus_tool]\n")
    assert has_error(lint_repo(repo), AGENT_FILE, "unknown tool: bogus_tool")


def test_lint_missing_instructions(repo):
    (repo / SYSTEM).unlink()
    assert has_error(lint_repo(repo), AGENT_FILE, "SYSTEM.md")


def test_lint_mcp_version_mismatch(repo):
    rewrite(repo / AGENT_FILE, lambda t: t.replace("mcp_servers: []", 'mcp_servers: ["fetch@0.9.0"]'))
    assert has_error(lint_repo(repo), AGENT_FILE, "MCP server fetch is version 0.3.0, agent requires 0.9.0")


def test_lint_network_open_server_without_permission(repo):
    rewrite(repo / AGENT_FILE, lambda t: t.replace("mcp_servers: []", 'mcp_servers: ["fetch@0.3.0"]'))
    assert has_error(lint_repo(repo), AGENT_FILE, "needs open network")


def test_lint_only_agent_limits_scope(repo):
    rewrite(repo / AGENT_FILE, lambda t: t + "tools: [bogus_tool]\n")
    other = repo / "agents" / "other"
    other.mkdir()
    (other / "agent.yaml").write_text("schema_version: maef.dev/v1alpha1\nid: other\n")
    everything = {f.path for f in lint_repo(repo)}
    assert {AGENT_FILE, "agents/other/agent.yaml"} <= everything
    only_other = {f.path for f in lint_repo(repo, only_agent="other")}
    assert only_other == {"agents/other/agent.yaml"}
    only_agent = {f.path for f in lint_repo(repo, only_agent=AGENT)}
    assert only_agent == {AGENT_FILE}


def test_cli_lint_exit_codes(repo, capsys):
    assert cli_main(["lint", str(repo)]) == 0
    rewrite(repo / AGENT_FILE, lambda t: t + "tools: [bogus_tool]\n")
    assert cli_main(["lint", str(repo)]) == 1
    assert "unknown tool: bogus_tool" in capsys.readouterr().out


# -- drafts --

def test_create_makes_branch_and_worktree(repo, store):
    draft = store.create(AGENT)
    assert re.fullmatch(r"maef/draft/research-analyst/[0-9a-f]{12}", draft.branch)
    assert branch_exists(repo, draft.branch)
    assert (tree(repo, draft) / AGENT_FILE).is_file()
    assert draft.base == head(repo)


def test_write_accepts_agent_file(repo, store):
    draft = store.create(AGENT)
    store.write(draft.id, SYSTEM, "New.\n")
    assert (tree(repo, draft) / SYSTEM).read_text() == "New.\n"


@pytest.mark.parametrize("path", [
    "../x", "/etc/passwd", "agents/other/x", ".hidden", f"{AGENT_DIR}/.hidden", f"{AGENT_DIR}/../../x",
])
def test_write_rejects_bad_paths(store, path):
    draft = store.create(AGENT)
    with pytest.raises(DraftRejected) as exc:
        store.write(draft.id, path, "x")
    assert exc.value.code == "invalid_path"


def test_write_rejects_symlink_paths(repo, store):
    draft = store.create(AGENT)
    base = tree(repo, draft) / AGENT_DIR
    os.symlink("SYSTEM.md", base / "link.md")
    os.symlink("..", base / "linkdir")
    for path in (f"{AGENT_DIR}/link.md", f"{AGENT_DIR}/linkdir/x.md"):
        with pytest.raises(DraftRejected) as exc:
            store.write(draft.id, path, "x")
        assert exc.value.code == "invalid_path"


def test_write_size_limit(store):
    draft = store.create(AGENT)
    store.write(draft.id, SYSTEM, "x" * MAX_FILE_BYTES)
    with pytest.raises(DraftRejected) as exc:
        store.write(draft.id, SYSTEM, "x" * (MAX_FILE_BYTES + 1))
    assert exc.value.code == "too_large"


def test_review_reports_diff_and_permission_changes(repo, store):
    base = (repo / AGENT_FILE).read_text()
    draft = store.create(AGENT)
    new = (base.replace("mcp_servers: []", 'mcp_servers: ["fetch@0.3.0"]')
           .replace("max_tokens: 200000", "max_tokens: 300000")
           + "tools: [current_time]\npermissions:\n  network_open: true\n")
    store.write(draft.id, AGENT_FILE, new)
    review = store.review(draft.id)
    assert "+tools: [current_time]" in review.diff
    assert review.files == [AGENT_FILE]
    p = review.permissions
    assert p.tools_added == ["current_time"]
    assert p.mcp_servers_added == ["fetch@0.3.0"]
    assert p.mcp_network_open_added == ["fetch@0.3.0"]
    assert p.network_open.before is False and p.network_open.after is True
    assert [c.field for c in p.budget_increases] == ["budget.max_tokens"]
    assert review.publishable is True


def test_publish_refuses_lint_errors(repo, store):
    draft = store.create(AGENT)
    store.write(draft.id, AGENT_FILE, (repo / AGENT_FILE).read_text() + "tools: [bogus_tool]\n")
    with pytest.raises(DraftRejected) as exc:
        store.publish(draft.id, "bad tool")
    assert exc.value.code == "lint_failed"


def test_publish_refuses_no_changes(store):
    draft = store.create(AGENT)
    with pytest.raises(DraftRejected) as exc:
        store.publish(draft.id, "nothing")
    assert exc.value.code == "no_changes"


def test_publish_commits_on_draft_branch_without_merging(repo, store, profiles):
    draft = store.create(AGENT)
    store.write(draft.id, SYSTEM, "Published instructions.\n")
    base_head = head(repo)
    sha = store.publish(draft.id, "Tighten prompt")
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert git(repo, "rev-parse", f"refs/heads/{draft.branch}") == sha
    assert head(repo) == base_head  # base branch untouched: no merge, no push
    pinned = definitions(repo, profiles).resolve(AGENT, sha)
    assert pinned.git_rev == sha
    assert pinned.instructions == "Published instructions.\n"
    assert store.list()[0].published == sha


def test_discard_unpublished_removes_worktree_and_branch(repo, store):
    draft = store.create(AGENT)
    assert tree(repo, draft).is_dir()
    store.discard(draft.id)
    assert not tree(repo, draft).exists()
    assert not branch_exists(repo, draft.branch)
    assert store.list() == []


def test_discard_published_keeps_branch(repo, store):
    draft = store.create(AGENT)
    store.write(draft.id, SYSTEM, "Kept.\n")
    sha = store.publish(draft.id, "keep me")
    store.discard(draft.id)
    assert not tree(repo, draft).exists()
    assert git(repo, "rev-parse", f"refs/heads/{draft.branch}") == sha


# -- HTTP --

@pytest.fixture
def http(repo, monkeypatch):
    monkeypatch.setenv("MAEF_AUTHORING_ENABLED", "1")
    monkeypatch.setattr(main, "drafts", Drafts(repo))
    return TestClient(main.app)


def test_draft_routes_disabled_without_env(http, monkeypatch):
    monkeypatch.delenv("MAEF_AUTHORING_ENABLED", raising=False)
    assert http.post("/api/v1/drafts", json={"agent_id": AGENT}).status_code == 501
    assert http.get("/api/v1/drafts").status_code == 501


def test_draft_routes_full_flow(http):
    created = http.post("/api/v1/drafts", json={"agent_id": AGENT})
    assert created.status_code == 201
    did = created.json()["id"]
    assert http.put(f"/api/v1/drafts/{did}/files/{SYSTEM}", content=b"Over HTTP.\n").status_code == 200
    review = http.get(f"/api/v1/drafts/{did}/review")
    assert review.status_code == 200 and "Over HTTP." in review.json()["diff"]
    published = http.post(f"/api/v1/drafts/{did}/publish", json={"message": "HTTP edit"})
    assert published.status_code == 200 and len(published.json()["revision"]) == 40
    assert http.delete(f"/api/v1/drafts/{did}").status_code == 200


def test_draft_route_errors(http):
    did = http.post("/api/v1/drafts", json={"agent_id": AGENT}).json()["id"]
    url = f"/api/v1/drafts/{did}/files/{SYSTEM}"
    assert http.put(url, content=b"x" * (MAX_FILE_BYTES + 1)).status_code == 413
    assert http.put(f"/api/v1/drafts/{did}/files/agents/other/x", content=b"x").status_code == 400
    assert http.put("/api/v1/drafts/000000000000/files/" + SYSTEM, content=b"x").status_code == 404
    assert http.get("/api/v1/drafts/000000000000/review").status_code == 404
    assert http.post(f"/api/v1/drafts/{did}/publish", json={"message": "nothing"}).status_code == 409


def test_schema_route(http):
    resp = http.get("/api/v1/schemas/agent-definition")
    assert resp.status_code == 200 and resp.json() == schema_for("agent-definition")
    assert http.get("/api/v1/schemas/nope").status_code == 404


# -- client and CLI --

class Recording(AsgiTransport):
    """AsgiTransport that remembers the JSON body of every POST /api/v1/runs."""

    def __init__(self, app):
        super().__init__(app)
        self.bodies = []

    def request(self, method, path, *, json=None, headers=None, data=None):
        if method == "POST" and path == "/api/v1/runs":
            self.bodies.append(json)
        return super().request(method, path, json=json, headers=headers, data=data)


def test_cli_draft_new_write_review_publish(repo, http, tmp_path, capsys):
    client = MaefClient(AsgiTransport(main.app))
    assert cli_main(["draft", "new", AGENT, "--json"], client=client) == 0
    draft = json.loads(capsys.readouterr().out)
    src = tmp_path / "system.md"
    src.write_text("Written from the CLI.\n")
    assert cli_main(["draft", "write", draft["id"], SYSTEM, str(src)], client=client) == 0
    capsys.readouterr()
    assert cli_main(["draft", "review", draft["id"], "--json"], client=client) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["publishable"] is True and "Written from the CLI." in review["diff"]
    assert cli_main(["draft", "publish", draft["id"], "-m", "CLI edit", "--json"], client=client) == 0
    sha = json.loads(capsys.readouterr().out)["revision"]
    assert git(repo, "rev-parse", draft["branch"]) == sha


def test_cli_run_revision_sends_agent_revision(repo, profiles, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MAEF_EXECUTION_ENABLED", "1")
    ledger = Ledger(tmp_path / "runs.db")
    ledger.ensure_account("default", 5_000_000)
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setattr(main, "runs", Runs(definitions(repo, profiles), ledger,
                                           execution_enabled=main.execution_enabled, allow_dirty=main.allow_dirty))
    sha = head(repo)
    transport = Recording(main.app)
    client = MaefClient(transport)
    assert cli_main(["run", AGENT, "Hello", "--revision", sha, "--json"], client=client) == 0
    run = json.loads(capsys.readouterr().out)
    assert transport.bodies[-1]["agent_revision"] == sha
    record = client.get_run(run["run_id"])
    assert record["git_rev"] == sha and record["dirty"] is False


def test_uncommitted_sandbox_profile_edit_marks_run_dirty(repo, profiles):
    defs = definitions(repo, profiles)
    assert defs.resolve("research-analyst").dirty is False
    manifest = repo / "sandbox-profiles" / "restricted-local.yaml"
    manifest.write_text(manifest.read_text() + "# local edit\n")
    assert defs.resolve("research-analyst").dirty is True
