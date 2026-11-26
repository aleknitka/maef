"""Git-backed drafts of agent definitions: edit on a branch in a worktree, review, publish a commit.

Interface: `Drafts(repo_root)` with `create`, `write`, `review`, `publish`, `discard`, `list`.
Invariants: a draft is branch `maef/draft/<agent>/<id>` plus a worktree under `runs/drafts/<id>`
(gitignored); only text files under `agents/<agent>/` can be written; publish commits on the draft
branch and returns the sha, it never merges or pushes (a human merges after review, ADR-0003); the
published sha can be run at once with `RunRequest.agent_revision`. Discarding a published draft removes the
worktree but keeps the branch, so the published commit stays reachable.
"""

import contextlib
import os
import re
import secrets
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from .gitrepo import GitError, GitRepo, run_git, safe_relpath
from .lint import Finding, lint_repo
from .models import AgentDefinition, McpServerManifest

MAX_FILE_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 2000
_DRAFT_ID = re.compile(r"^[0-9a-f]{12}$")
_AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class DraftError(Exception):
    pass


class DraftNotFound(DraftError):
    pass


class DraftRejected(DraftError):
    """The request is refused; `code` is machine-readable (invalid_path, too_large, lint_failed, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Draft(BaseModel):
    id: str
    agent_id: str
    branch: str
    base: str  # commit the draft started from
    created_at: str
    new_agent: bool = False
    published: str | None = None  # sha of the publish commit


class Change(BaseModel):
    field: str
    before: str | int | bool | None
    after: str | int | bool | None


class PermissionDiff(BaseModel):
    new_agent: bool = False
    tools_added: list[str] = []
    tools_removed: list[str] = []
    mcp_servers_added: list[str] = []
    mcp_servers_removed: list[str] = []
    network_open: Change | None = None
    budget_increases: list[Change] = []
    runtime_changes: list[Change] = []
    # Per referenced MCP server: secrets and open network it did not have at the base revision.
    mcp_new_secrets: dict[str, list[str]] = {}
    mcp_network_open_added: list[str] = []

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.tools_added,
                self.tools_removed,
                self.mcp_servers_added,
                self.mcp_servers_removed,
                self.network_open,
                self.budget_increases,
                self.runtime_changes,
                self.mcp_new_secrets,
                self.mcp_network_open_added,
            )
        )


class Review(BaseModel):
    draft_id: str
    base: str
    diff: str
    files: list[str]
    permissions: PermissionDiff
    findings: list[Finding]
    publishable: bool


def _agent(data: bytes | None) -> AgentDefinition | None:
    if data is None:
        return None
    try:
        return AgentDefinition.model_validate(yaml.safe_load(data))
    except (yaml.YAMLError, ValidationError, UnicodeDecodeError):
        return None


def _manifest(data: bytes | None) -> McpServerManifest | None:
    if data is None:
        return None
    try:
        return McpServerManifest.model_validate(yaml.safe_load(data))
    except (yaml.YAMLError, ValidationError, UnicodeDecodeError):
        return None


def _mcp_id(ref: str) -> str:
    return ref.partition("@")[0]


def permission_diff(
    old: AgentDefinition | None,
    new: AgentDefinition | None,
    old_manifests: dict[str, McpServerManifest],
    new_manifests: dict[str, McpServerManifest],
) -> PermissionDiff:
    """What a reviewer must approve: capability the new definition gains or changes versus the old one."""
    if new is None:
        return PermissionDiff(new_agent=old is None)
    diff = PermissionDiff(new_agent=old is None)
    old_tools, old_mcp = (set(old.tools), set(old.mcp_servers)) if old else (set(), set())
    diff.tools_added = sorted(set(new.tools) - old_tools)
    diff.tools_removed = sorted(old_tools - set(new.tools))
    diff.mcp_servers_added = sorted(set(new.mcp_servers) - old_mcp)
    diff.mcp_servers_removed = sorted(old_mcp - set(new.mcp_servers))
    before_open = old.permissions.network_open if old else False
    if new.permissions.network_open != before_open:
        diff.network_open = Change(
            field="permissions.network_open", before=before_open, after=new.permissions.network_open
        )
    old_budget = old.budget if old else None
    for name in ("max_tokens", "max_duration_seconds", "max_turns"):
        after, before = getattr(new.budget, name), getattr(old_budget, name) if old_budget else None
        if before is None or after > before:
            diff.budget_increases.append(Change(field=f"budget.{name}", before=before, after=after))
    for name in ("model_profile", "sandbox_profile"):
        after, before = getattr(new.runtime, name), getattr(old.runtime, name) if old else None
        if after != before:
            diff.runtime_changes.append(Change(field=f"runtime.{name}", before=before, after=after))
    for ref in new.mcp_servers:
        server = _mcp_id(ref)
        manifest = new_manifests.get(server)
        if manifest is None:
            continue
        previous = old_manifests.get(server) if old and any(_mcp_id(r) == server for r in old.mcp_servers) else None
        new_secrets = sorted(set(manifest.secrets) - set(previous.secrets if previous else ()))
        if new_secrets:
            diff.mcp_new_secrets[ref] = new_secrets
        if manifest.network != "none" and not (previous and previous.network == manifest.network):
            diff.mcp_network_open_added.append(ref)
    return diff


class Drafts:
    def __init__(self, repo_root: Path | str, drafts_dir: Path | str | None = None):
        self.repo = GitRepo(repo_root)
        self.root = self.repo.root
        self.dir = Path(drafts_dir) if drafts_dir else self.root / "runs" / "drafts"
        self._lock = threading.RLock()

    # -- storage --
    def _meta(self, draft_id: str) -> Path:
        return self.dir / f"{draft_id}.json"

    def _tree(self, draft_id: str) -> Path:
        return self.dir / draft_id

    def _load(self, draft_id: str) -> Draft:
        if not _DRAFT_ID.fullmatch(draft_id or ""):
            raise DraftNotFound(draft_id)
        try:
            return Draft.model_validate_json(self._meta(draft_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DraftNotFound(draft_id) from None

    def _save(self, draft: Draft) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._meta(draft.id).with_suffix(".tmp")
        tmp.write_text(draft.model_dump_json(), encoding="utf-8")
        os.replace(tmp, self._meta(draft.id))

    def _git(self, *args: str, cwd: Path | None = None, **kw) -> bytes:
        try:
            return run_git(cwd or self.root, *args, **kw)
        except GitError as exc:
            raise DraftRejected("git_error", str(exc)) from None

    # -- interface --
    def create(self, agent_id: str | None = None, new_agent_id: str | None = None) -> Draft:
        """A branch from HEAD and a worktree for editing `agent_id`, or for a new agent `new_agent_id`."""
        if bool(agent_id) == bool(new_agent_id):
            raise DraftRejected("invalid_request", "Give exactly one of agent_id or new_agent_id.")
        target = agent_id or new_agent_id
        if not target or not _AGENT_ID.fullmatch(target):
            raise DraftRejected("invalid_request", "Agent id must match ^[a-z][a-z0-9-]{1,62}$.")
        with self._lock:
            try:
                base = self.repo.resolve_revision("HEAD")
                exists = self.repo.read_blob(base, f"agents/{target}/agent.yaml") is not None
            except GitError as exc:
                raise DraftRejected("git_error", str(exc)) from None
            if agent_id and not exists:
                raise DraftRejected("unknown_agent", f"Agent not found at HEAD: {agent_id}")
            if new_agent_id and exists:
                raise DraftRejected("agent_exists", f"Agent already exists: {new_agent_id}")
            draft_id = secrets.token_hex(6)
            branch = f"maef/draft/{target}/{draft_id}"
            self.dir.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "-q", "-b", branch, str(self._tree(draft_id)), base)
            draft = Draft(
                id=draft_id,
                agent_id=target,
                branch=branch,
                base=base,
                new_agent=not exists,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            self._save(draft)
            return draft

    def list(self) -> list[Draft]:
        drafts = []
        for path in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []:
            try:
                drafts.append(self._load(path.stem))
            except DraftNotFound:
                continue
        return sorted(drafts, key=lambda d: d.created_at)

    def write(self, draft_id: str, relpath: str, content: str) -> None:
        """Create or replace one text file under `agents/<agent>/` in the draft's worktree."""
        draft = self._load(draft_id)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise DraftRejected("too_large", f"File exceeds {MAX_FILE_BYTES // 1024} KiB.")
        if "\0" in content:
            raise DraftRejected("invalid_content", "Binary content is not allowed.")
        try:
            safe_relpath(relpath)
        except ValueError:
            raise DraftRejected("invalid_path", "Path must be relative with no '..' segments.") from None
        parts = relpath.split("/")
        prefix = ["agents", draft.agent_id]
        if parts[:2] != prefix or len(parts) < 3 or len(parts) > 8 or not all(_SEGMENT.fullmatch(p) for p in parts[2:]):
            raise DraftRejected("invalid_path", f"Only files under agents/{draft.agent_id}/ can be written.")
        tree = self._tree(draft_id)
        with self._lock:
            if not tree.is_dir():
                raise DraftNotFound(draft_id)
            current = tree
            for part in parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise DraftRejected("invalid_path", "Symlinks are not allowed.")
                current.mkdir(exist_ok=True)
            target = current / parts[-1]
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise DraftRejected("invalid_path", "Symlinks and directories cannot be overwritten.")
            if not target.resolve().is_relative_to((tree / "agents" / draft.agent_id).resolve()):
                raise DraftRejected("invalid_path", "Path escapes the agent directory.")
            tmp = target.with_name(f".{target.name}.maef-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)

    def _stage(self, draft: Draft) -> None:
        self._git("add", "-A", "--", f"agents/{draft.agent_id}", cwd=self._tree(draft.id))

    def review(self, draft_id: str) -> Review:
        draft = self._load(draft_id)
        tree = self._tree(draft_id)
        with self._lock:
            if not tree.is_dir():
                raise DraftNotFound(draft_id)
            self._stage(draft)
            scope = f"agents/{draft.agent_id}"
            diff = self._git(
                "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", draft.base, "--", scope, cwd=tree
            ).decode("utf-8", errors="replace")
            names = self._git("diff", "--cached", "--name-only", "-z", draft.base, "--", scope, cwd=tree).decode(
                "utf-8", errors="replace"
            )
            findings = lint_repo(tree, only_agent=draft.agent_id)
            permissions = self._permissions(draft, tree)
        return Review(
            draft_id=draft.id,
            base=draft.base,
            diff=diff,
            files=[n for n in names.split("\0") if n],
            permissions=permissions,
            findings=findings,
            publishable=not any(f.severity == "error" for f in findings),
        )

    def _permissions(self, draft: Draft, tree: Path) -> PermissionDiff:
        base_agent = _agent(self._blob(draft.base, f"agents/{draft.agent_id}/agent.yaml"))
        try:
            new_agent = _agent((tree / "agents" / draft.agent_id / "agent.yaml").read_bytes())
        except OSError:
            new_agent = None
        old_m: dict[str, McpServerManifest] = {}
        new_m: dict[str, McpServerManifest] = {}
        for agent, found, read in (
            (base_agent, old_m, lambda i: self._blob(draft.base, f"mcp-servers/{i}.yaml")),
            (new_agent, new_m, lambda i: _read_file(tree / "mcp-servers" / f"{i}.yaml")),
        ):
            for ref in agent.mcp_servers if agent else ():
                server = _mcp_id(ref)
                if _SEGMENT.fullmatch(server) and (m := _manifest(read(server))):
                    found[server] = m
        return permission_diff(base_agent, new_agent, old_m, new_m)

    def _blob(self, sha: str, relpath: str) -> bytes | None:
        try:
            return self.repo.read_blob(sha, relpath)
        except GitError:
            return None

    def publish(self, draft_id: str, message: str) -> str:
        """Commit the draft's changes on its branch and return the sha. Refused when lint finds errors or
        nothing changed. Does not merge or push."""
        message = (message or "").strip()
        if not message or len(message) > MAX_MESSAGE_CHARS:
            raise DraftRejected("invalid_message", f"Commit message must be 1-{MAX_MESSAGE_CHARS} characters.")
        draft = self._load(draft_id)
        tree = self._tree(draft_id)
        with self._lock:
            if not tree.is_dir():
                raise DraftNotFound(draft_id)
            errors = [f for f in lint_repo(tree, only_agent=draft.agent_id) if f.severity == "error"]
            if errors:
                shown = "; ".join(str(f) for f in errors[:5]) + (
                    f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
                )
                raise DraftRejected("lint_failed", f"Lint errors: {shown}")
            self._stage(draft)
            if not self._git("diff", "--cached", "--name-only", "HEAD", "--", f"agents/{draft.agent_id}", cwd=tree):
                raise DraftRejected("no_changes", "The draft has no changes to publish.")
            name = os.environ.get("MAEF_AUTHORING_NAME", "MAEF Authoring")
            email = os.environ.get("MAEF_AUTHORING_EMAIL", "maef@localhost")
            ident = {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
            }
            self._git(
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "--no-verify",
                "-F",
                "-",
                cwd=tree,
                input=message.encode("utf-8"),
                env=ident,
            )
            sha = self._git("rev-parse", "HEAD", cwd=tree).decode().strip()
            draft.published = sha
            self._save(draft)
            return sha

    def discard(self, draft_id: str) -> None:
        """Remove the worktree and record; the branch too, unless the draft was published."""
        draft = self._load(draft_id)
        with self._lock:
            tree = self._tree(draft_id)
            if tree.is_dir():
                try:
                    self._git("worktree", "remove", "--force", str(tree))
                except DraftRejected:
                    shutil.rmtree(tree, ignore_errors=True)
                    self._git("worktree", "prune")
            if draft.published is None:
                with contextlib.suppress(DraftRejected):  # branch may already be gone
                    self._git("branch", "-D", draft.branch)
            self._meta(draft_id).unlink(missing_ok=True)


def _read_file(path: Path) -> bytes | None:
    try:
        return None if path.is_symlink() else path.read_bytes()
    except OSError:
        return None
