"""Credential references resolved at call time. Values never leave a Secret except via reveal()."""

import fnmatch
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import yaml

log = logging.getLogger("maef.credentials")
_ENV_REF = re.compile(r"^env:([A-Z][A-Z0-9_]{1,63})$")


class Secret:
    """Opaque wrapper: repr, str and format all print a mask; compare by identity only."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name, value):
        raise AttributeError("Secret is immutable")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "Secret(***)"

    __hash__ = object.__hash__


class CredentialError(Exception):
    """Names the variable or reference only, never a value."""


class CredentialNotAllowed(CredentialError):
    """The reference names an environment variable this process is not allowed to read."""


class CredentialEndpointMismatch(CredentialNotAllowed):
    """The credential is allowed, but not for the endpoint the lease's profile points at."""


def referenced_env_names(root: Path) -> set[str]:
    """Environment variable names that the project's own model profiles and MCP manifests reference
    (`credential_ref: env:NAME` and `secrets: {X: env:NAME}`). Unreadable or malformed files are skipped."""
    names: set[str] = set()
    for folder in ("model-profiles", "mcp-servers"):
        for path in sorted((root / folder).glob("*.y*ml")) if (root / folder).is_dir() else []:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            if not isinstance(data, dict):
                continue
            refs = (
                [data.get("credential_ref"), *(data.get("secrets") or {}).values()]
                if isinstance(data.get("secrets") or {}, dict)
                else [data.get("credential_ref")]
            )
            for ref in refs:
                m = _ENV_REF.match(ref) if isinstance(ref, str) else None
                if m:
                    names.add(m.group(1))
    return names


def endpoint_destination(endpoint) -> tuple:
    """Where a model profile sends its credential: kind plus base_url, region or account. The model id is
    left out on purpose; changing the model does not change who receives the key."""
    return tuple(getattr(endpoint, a, None) for a in ("kind", "base_url", "region", "account_id"))


def profile_endpoints(root: Path | None) -> dict[str, set[tuple]]:
    """env credential name -> destinations of the project's own model profiles that reference it."""
    from .models import ModelEndpointProfile

    found: dict[str, set[tuple]] = {}
    folder = root / "model-profiles" if root is not None else None
    for path in sorted(folder.glob("*.y*ml")) if folder is not None and folder.is_dir() else []:
        try:
            profile = ModelEndpointProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
            continue
        m = _ENV_REF.match(profile.credential_ref)
        if m:
            found.setdefault(m.group(1), set()).add(endpoint_destination(profile.endpoint))
    return found


def explicit_credentials(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names or globs the operator listed in MAEF_WORKER_ALLOWED_CREDENTIALS (never bound to an endpoint)."""
    env = os.environ if environ is None else environ
    return [p.strip() for p in env.get("MAEF_WORKER_ALLOWED_CREDENTIALS", "").split(",") if p.strip()]


def worker_allowed_credentials(root: Path | None, environ: Mapping[str, str] | None = None) -> set[str]:
    """Patterns (names or simple `*` globs) a worker may resolve: what the project's model profiles and MCP
    manifests reference, plus the comma-separated `MAEF_WORKER_ALLOWED_CREDENTIALS`. Anything else is refused,
    so a lease cannot make the worker read an arbitrary variable (for example its own worker token)."""
    env = os.environ if environ is None else environ
    allowed = referenced_env_names(root) if root is not None else set()
    allowed.update(p.strip() for p in env.get("MAEF_WORKER_ALLOWED_CREDENTIALS", "").split(",") if p.strip())
    return allowed


class Credentials:
    """`allowed` (worker side): None allows any `env:` name; otherwise an iterable of names or globs, or a
    callable returning one (re-evaluated on every resolve so new project files are picked up)."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        allowed: Iterable[str] | Callable[[], Iterable[str]] | None = None,
        *,
        explicit: Callable[[], Iterable[str]] | None = None,
        bound: Callable[[], Mapping[str, set[tuple]]] | None = None,
    ):
        """`bound` (name -> permitted destinations) and `explicit` (operator-listed names, exempt) turn on
        endpoint binding for `check(ref, endpoint=...)`."""
        self._environ = os.environ if environ is None else environ
        self._allowed = allowed
        self._explicit, self._bound = explicit, bound

    def check(self, ref: str, endpoint=None) -> None:
        """Raise CredentialNotAllowed when `ref` is an `env:` reference outside the allowlist. Never reads the value."""
        scheme, _, name = ref.partition(":")
        if scheme != "env" or not name or self._allowed is None:
            return
        source = self._allowed
        patterns: list[str] = list(source() if callable(source) else source)  # ty: ignore[invalid-argument-type]
        if not any(fnmatch.fnmatchcase(name, p) for p in patterns):
            log.error(
                "credential reference refused: env:%s is not allowed on this worker "
                "(add it to MAEF_WORKER_ALLOWED_CREDENTIALS or reference it from a local profile or MCP manifest)",
                name,
            )
            raise CredentialNotAllowed(f"credential_not_allowed:{name}")
        if endpoint is None or self._bound is None:
            return
        if any(fnmatch.fnmatchcase(name, p) for p in (self._explicit() if self._explicit else ())):
            return  # the operator named it explicitly: unbound
        if endpoint_destination(endpoint) not in self._bound().get(name, set()):
            log.error(
                "credential reference refused: env:%s is not used by any local model profile with this endpoint",
                name,
            )
            raise CredentialEndpointMismatch(f"credential_endpoint_mismatch:{name}")

    def resolve(self, ref: str) -> Secret | None:
        """none:local -> None (keyless server). env:NAME -> Secret. aws:default-chain -> None:
        Bedrock authenticates through the boto3 default chain itself, so there is no value to hand out."""
        scheme, _, name = ref.partition(":")
        if ref in ("none:local", "aws:default-chain"):
            return None
        if scheme == "env" and name:
            self.check(ref)
            value = self._environ.get(name)
            if not value:
                raise CredentialError(f"missing_credential:{name}")
            return Secret(value)
        raise CredentialError(f"unsupported_credential_ref:{scheme}")
