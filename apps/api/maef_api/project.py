"""Where the project lives: the folder holding agents/, sandbox-profiles/, model-profiles/, runs/ and artifacts/.

Resolved lazily, on each call, so importing the package never touches the filesystem or the environment."""

import os
from pathlib import Path

MARKER = "maef.toml"
_CHECKOUT = Path(__file__).resolve().parents[3]


class ProjectNotFoundError(Exception):
    pass


def source_checkout() -> Path | None:
    """The repository this package runs from, when it is a source checkout (editable install or `pythonpath`)."""
    if (_CHECKOUT / "agents").is_dir() and (_CHECKOUT / "pyproject.toml").is_file():
        return _CHECKOUT
    return None


def _marked_parent(start: Path) -> Path | None:
    return next((p for p in (start, *start.parents) if (p / MARKER).is_file()), None)


def project_root() -> Path:
    """`MAEF_HOME`; else the nearest parent of the current directory holding `maef.toml`; else the source
    checkout this package runs from; else an error that points at `maef init`."""
    home = os.environ.get("MAEF_HOME")
    if home:
        return Path(home).expanduser().resolve()
    found = _marked_parent(Path.cwd().resolve())
    if found is not None:
        return found
    checkout = source_checkout()
    if checkout is not None:
        return checkout
    raise ProjectNotFoundError(
        "no MAEF project here: no maef.toml in this folder or any parent, and MAEF_HOME is not set. "
        "Run `maef init` in an empty folder to create one."
    )


def bundled_dir() -> Path:
    """The read-only starter files shipped with the package (same layout as the repository). In a source
    checkout without a built wheel these are the repository's own folders."""
    packaged = Path(__file__).resolve().parent / "_bundled"
    if packaged.is_dir():
        return packaged
    checkout = source_checkout()
    if checkout is not None:
        return checkout
    raise ProjectNotFoundError("the bundled starter files are missing from this installation")
