"""Principals, tokens and scopes (BACKEND_PLAN step 11).

API tokens are `maef_<32 random bytes, urlsafe>` and worker credentials `maefw_<...>`. They are shown
once at creation; the ledger stores only their sha256. Verification looks the hash up, then compares
it in constant time. `admin` implies every scope. Nothing here logs or returns a token after issue.
"""

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from .ledger import Ledger
from .registry import ROOT

API_PREFIX = "maef_"
WORKER_PREFIX = "maefw_"

SCOPE_READ = "read"
SCOPE_RUNS = "runs:write"
SCOPE_DECISIONS = "decisions:write"
SCOPE_AUTHORING = "authoring"
SCOPE_ADMIN = "admin"
SCOPES = (SCOPE_READ, SCOPE_RUNS, SCOPE_DECISIONS, SCOPE_AUTHORING, SCOPE_ADMIN)

DEV_PRINCIPAL = "dev-unauthenticated"
ANONYMOUS = "anonymous"

MISSING_TOKEN_HINT = "Set MAEF_API_TOKEN (create one with `maef admin token create`)."


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    name: str  # token:<id> | dev-unauthenticated | anonymous
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        return SCOPE_ADMIN in self.scopes or scope in self.scopes


def parse_scopes(text: str) -> tuple[str, ...]:
    scopes = tuple(dict.fromkeys(s.strip() for s in text.split(",") if s.strip()))
    unknown = [s for s in scopes if s not in SCOPES]
    if not scopes or unknown:
        raise AuthError(
            f"scopes must be a comma list of: {', '.join(SCOPES)}"
            + (f" (unknown: {', '.join(unknown)})" if unknown else "")
        )
    return scopes


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def issue_api_token(ledger: Ledger, name: str, scopes: tuple[str, ...]) -> tuple[str, str]:
    """-> (token id, token). The token is returned once; only its hash is stored."""
    if not name.strip():
        raise AuthError("name must not be empty")
    token = new_token(API_PREFIX)
    return ledger.register_api_token(name.strip(), scopes, hash_token(token)), token


def issue_worker_credential(ledger: Ledger, worker_id: str) -> str:
    if not worker_id.strip() or len(worker_id) > 200:
        raise AuthError("worker id must be 1-200 characters")
    token = new_token(WORKER_PREFIX)
    ledger.register_worker_credential(worker_id, hash_token(token))
    return token


def bearer(authorization: str | None) -> str | None:
    scheme, _, value = (authorization or "").partition(" ")
    value = value.strip()
    return value if scheme.lower() == "bearer" and value else None


def verify_api_token(ledger: Ledger, token: str | None) -> Principal | None:
    if not token or not token.startswith(API_PREFIX):
        return None
    digest = hash_token(token)
    row = ledger.api_token_by_hash(digest)
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None
    return Principal(f"token:{row['id']}", frozenset(row["scopes"]))


def verify_worker_token(ledger: Ledger, token: str | None) -> str | None:
    """The worker_id the credential belongs to, or None."""
    if not token or not token.startswith(WORKER_PREFIX):
        return None
    digest = hash_token(token)
    row = ledger.worker_by_hash(digest)
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None
    return row["worker_id"]


def local_ledger() -> Ledger:
    """The ledger the API uses (MAEF_DB), opened directly: the bootstrap CLI is the root of trust."""
    return Ledger.open(os.environ.get("MAEF_DB", ROOT / "runs" / "maef.db"))
