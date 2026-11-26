"""Artifacts: content-addressed outputs of a run.

`ArtifactStore` is the seam: bytes in, an `ArtifactRef` out, bytes back only after the sha256
is re-verified. `ArtifactIntake` is the single entry point for new artifacts (the worker's
LocalChannel, the HTTP upload route and the in-process LedgerSink all use it), so size limits,
digest checks and fencing are enforced in one place. Nothing here knows about SQL; the ledger
records refs through the `ArtifactLog` protocol.
"""

import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, field_validator

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_RUN_ARTIFACTS = 32
MAX_RUN_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_NAME_CHARS = 128

SAFE_MIMES = (
    "text/markdown",
    "text/plain",
    "application/json",
    "text/csv",
    "image/png",
    "image/jpeg",
    "application/pdf",
    "application/octet-stream",
)
DEFAULT_MIME = "application/octet-stream"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE_NAME = re.compile(r"[^\w.\- ()+,@=~]")


class ArtifactError(Exception):
    """Base for artifact failures. `code` is stable and safe to show; messages never carry content."""

    code = "artifact_error"

    def __init__(self, code: str | None = None):
        self.code = code or type(self).code
        super().__init__(self.code)


class ArtifactNotFound(ArtifactError):
    code = "not_found"


class ArtifactCorrupt(ArtifactError):
    code = "corrupt"


class ArtifactRejected(ArtifactError):
    """Refused at intake and not retryable: `too_large`, `digest_mismatch`, `run_limit`."""

    code = "rejected"


def clean_name(name: str) -> str:
    """Basename only: no separators, control characters or odd punctuation; at most 128 characters."""
    base = re.split(r"[\\/]", str(name))[-1]
    base = _UNSAFE_NAME.sub("_", "".join(c for c in base if c.isprintable()))
    base = base.strip(" .")[:MAX_NAME_CHARS].strip(" .")
    return base or "artifact"


def clean_mime(mime: str | None) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    return base if base in SAFE_MIMES else DEFAULT_MIME


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ArtifactRef(BaseModel):
    digest: str
    size: int
    mime: str = DEFAULT_MIME
    name: str

    @field_validator("digest")
    @classmethod
    def _digest(cls, v: str) -> str:
        if not DIGEST_RE.match(v):
            raise ValueError("digest must be sha256:<64 hex>")
        return v

    @field_validator("size")
    @classmethod
    def _size(cls, v: int) -> int:
        if v < 0:
            raise ValueError("size must not be negative")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_name(v)

    @field_validator("mime")
    @classmethod
    def _mime(cls, v: str) -> str:
        return clean_mime(v)


class ArtifactStore(Protocol):
    def put(self, data: bytes, *, name: str, mime: str) -> ArtifactRef: ...

    def read(self, ref_or_digest: "ArtifactRef | str") -> bytes:
        """Raises ArtifactNotFound, or ArtifactCorrupt when the bytes no longer hash to the digest."""

    def exists(self, digest: str) -> bool: ...


def _digest_of(ref_or_digest: "ArtifactRef | str") -> str:
    digest = ref_or_digest.digest if isinstance(ref_or_digest, ArtifactRef) else ref_or_digest
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise ArtifactNotFound()
    return digest


def _verified(digest: str, data: bytes) -> bytes:
    if sha256_digest(data) != digest:
        raise ArtifactCorrupt()
    return data


class InMemoryArtifactStore:
    def __init__(self):
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes, *, name: str, mime: str) -> ArtifactRef:
        ref = ArtifactRef(digest=sha256_digest(data), size=len(data), mime=mime, name=name)
        with self._lock:
            self._blobs.setdefault(ref.digest, bytes(data))
        return ref

    def read(self, ref_or_digest: ArtifactRef | str) -> bytes:
        digest = _digest_of(ref_or_digest)
        with self._lock:
            data = self._blobs.get(digest)
        if data is None:
            raise ArtifactNotFound()
        return _verified(digest, data)

    def exists(self, digest: str) -> bool:
        with self._lock:
            return digest in self._blobs


class LocalArtifactStore:
    """`<root>/sha256/<aa>/<rest>`. Writes go to a temp file in the target directory, then os.replace."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        hex_ = digest.removeprefix("sha256:")
        return self.root / "sha256" / hex_[:2] / hex_[2:]

    def put(self, data: bytes, *, name: str, mime: str) -> ArtifactRef:
        ref = ArtifactRef(digest=sha256_digest(data), size=len(data), mime=mime, name=name)
        path = self._path(ref.digest)
        if path.is_file():
            return ref  # deduplicated
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return ref

    def read(self, ref_or_digest: ArtifactRef | str) -> bytes:
        digest = _digest_of(ref_or_digest)
        try:
            data = self._path(digest).read_bytes()
        except FileNotFoundError:
            raise ArtifactNotFound() from None
        return _verified(digest, data)

    def exists(self, digest: str) -> bool:
        return bool(DIGEST_RE.match(digest)) and self._path(digest).is_file()


def default_artifact_store() -> "ArtifactStore":
    """`MAEF_ARTIFACTS_URL=s3://bucket/prefix` selects the S3 store (MAEF_S3_ENDPOINT_URL, MAEF_S3_REGION,
    MAEF_S3_ADDRESSING_STYLE, MAEF_S3_SSE=0 to disable SSE, MAEF_S3_ALLOW_PRIVATE=1, MAEF_S3_ACCESS_KEY_REF /
    MAEF_S3_SECRET_KEY_REF as env: refs); otherwise the local directory store."""
    url = os.environ.get("MAEF_ARTIFACTS_URL")
    if url:
        from urllib.parse import urlparse

        from .artifacts_s3 import DEFAULT_CREDENTIALS_REF, S3ArtifactStore

        parsed = urlparse(url)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError("MAEF_ARTIFACTS_URL must look like s3://bucket/prefix")
        env = os.environ.get
        return S3ArtifactStore(
            parsed.netloc,
            prefix=parsed.path.lstrip("/") or "artifacts/",
            endpoint_url=env("MAEF_S3_ENDPOINT_URL") or None,
            region=env("MAEF_S3_REGION") or None,
            addressing_style=env("MAEF_S3_ADDRESSING_STYLE") or "auto",
            sse=env("MAEF_S3_SSE", "1").lower() not in ("0", "false", "no", "off"),
            allow_private=env("MAEF_S3_ALLOW_PRIVATE") == "1",
            credentials_ref=env("MAEF_S3_ACCESS_KEY_REF") or DEFAULT_CREDENTIALS_REF,
            secret_ref=env("MAEF_S3_SECRET_KEY_REF") or None,
        )
    from .registry import ROOT

    return LocalArtifactStore(os.environ.get("MAEF_ARTIFACTS_DIR") or ROOT / "artifacts")


class ArtifactLog(Protocol):
    """The ledger's side of intake. `token=None` skips fencing (in-process, single-host use)."""

    def check_artifact_room(
        self, run_id: str, token: int | None, size: int, *, digest: str | None = None, name: str | None = None
    ) -> None:
        """Raises FencingError, or ArtifactRejected('run_limit')."""

    def add_artifact(self, run_id: str, token: int | None, ref: ArtifactRef) -> None: ...


class ArtifactIntake:
    """Validate, store, then record. The pre-check keeps a stale or over-limit writer from
    leaving blobs behind; recording re-checks inside its own transaction."""

    def __init__(self, log: ArtifactLog, store: ArtifactStore):
        self._log, self._store = log, store

    def accept(
        self,
        run_id: str,
        *,
        token: int | None,
        name: str,
        mime: str,
        data: bytes,
        digest: str | None = None,
        worker_id: str | None = None,
    ) -> ArtifactRef:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactRejected("too_large")
        if digest is not None and digest != sha256_digest(data):
            raise ArtifactRejected("digest_mismatch")
        # A retry of an already-recorded upload is a no-op, even when the run is at its limit.
        self._log.check_artifact_room(
            run_id, token, len(data), digest=sha256_digest(data), name=clean_name(name), worker_id=worker_id
        )
        ref = self._store.put(data, name=name, mime=mime)
        self._log.add_artifact(run_id, token, ref, worker_id=worker_id)
        return ref
