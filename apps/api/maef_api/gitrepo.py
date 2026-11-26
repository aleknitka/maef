"""Git access by argv list (never a shell): resolve a revision to a commit sha and read files from
the object store without touching the working tree.

Interface: `run_git(root, *args)`, `safe_relpath(path)`, `GitRepo(root)` with `toplevel()`,
`resolve_revision(revision)` and `read_blob(sha, relpath)`. Paths are repository-relative with `/`,
never absolute, empty, `.` or `..`; only regular files (not symlinks) are readable.
"""

import os
import re
import subprocess
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# A branch or tag name, not a revision expression: no `~^:@{` or spaces, never leading `-`.
REF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REGULAR_MODES = ("100644", "100755")


class GitError(Exception):
    """A git command failed or the request was not a safe revision/path."""


def run_git(
    root: Path | str, *args: str, input: bytes | None = None, env: dict[str, str] | None = None, timeout: float = 30
) -> bytes:
    """Run `git -C root args...` and return stdout; GitError carries a short stderr message."""
    full_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(  # noqa: S603  argv list, no shell; fixed container-runtime binary, no host shell
            ["git", "-C", str(root), *args],  # noqa: S607  fixed 'git' executable resolved from PATH by design
            capture_output=True,
            input=input,
            timeout=timeout,
            env=full_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise GitError("git is unavailable or timed out") from None
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", errors="replace").strip()[:300] or "git failed")
    return proc.stdout


def safe_relpath(path: str) -> str:
    """The path unchanged if it is a plain repository-relative path, else ValueError."""
    if not path or len(path) > 300 or "\0" in path or "\\" in path or path.startswith("/"):
        raise ValueError(f"unsafe path: {path[:80]!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError(f"unsafe path: {path[:80]!r}")
    return path


class GitRepo:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def toplevel(self) -> Path:
        return Path(run_git(self.root, "rev-parse", "--show-toplevel").decode().strip()).resolve()

    def resolve_revision(self, revision: str) -> str:
        """A 40-hex commit sha, or a branch/tag name (or HEAD), to the commit sha it names now."""
        if SHA_RE.fullmatch(revision):
            candidates = [revision]
        elif revision == "HEAD":
            candidates = ["HEAD"]
        elif REF_NAME_RE.fullmatch(revision):
            candidates = [f"refs/heads/{revision}", f"refs/tags/{revision}"]
        else:
            raise GitError(f"not a commit sha, branch or tag name: {revision[:80]!r}")
        for candidate in candidates:
            try:
                out = run_git(self.root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").decode().strip()
            except GitError:
                continue
            if SHA_RE.fullmatch(out):
                return out
        raise GitError(f"unknown revision: {revision[:80]}")

    def read_blob(self, sha: str, relpath: str) -> bytes | None:
        """Bytes of a regular file at `sha`, None if the path does not exist there; GitError for a
        symlink, directory or submodule."""
        if not SHA_RE.fullmatch(sha):
            raise GitError("not a commit sha")
        try:
            safe_relpath(relpath)
        except ValueError as exc:
            raise GitError(str(exc)) from None
        out = run_git(self.root, "ls-tree", "-z", sha, "--", relpath)
        if not out:
            return None
        meta, _, _name = out.rstrip(b"\0").decode("utf-8", errors="replace").partition("\t")
        fields = meta.split()
        if len(fields) != 3 or fields[1] != "blob" or fields[0] not in _REGULAR_MODES:
            raise GitError(f"not a regular file: {relpath}")
        return run_git(self.root, "cat-file", "blob", fields[2])
