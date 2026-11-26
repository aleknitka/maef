"""WorkerChannel: how a worker talks to the control plane. One interface, two adapters.

LocalChannel calls the Ledger directly (tests, single-host). HttpChannel speaks to the
`/api/v1/worker/` routes with urllib, so a worker needs nothing but outbound HTTPS.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlencode, urlparse

from .adapters.http import opener
from .artifacts import (
    ArtifactIntake,
    ArtifactRef,
    ArtifactRejected,
    ArtifactStore,
    InMemoryArtifactStore,
    sha256_digest,
)
from .credentials import Secret
from .egress import EgressDenied, EgressPolicy
from .leases import ChannelError, FencingError, Lease, SequenceGap
from .ledger import Ledger
from .models import is_loopback_host


class WorkerChannel(Protocol):
    def claim(self, worker_id: str) -> Lease | None: ...

    def heartbeat(self, lease: Lease) -> bool:
        """Returns whether cancellation was requested. Raises FencingError when the lease is no longer this worker's."""

    def report(
        self, lease: Lease, wseq: int, type: str, payload: dict | None = None, status: str | None = None
    ) -> bool:
        """Returns whether cancellation was requested. Raises FencingError or SequenceGap; a duplicate
        wseq is ignored."""

    def upload(self, lease: Lease, *, name: str, mime: str, data: bytes) -> ArtifactRef:
        """Raises FencingError, ChannelError (retryable) or ArtifactRejected (not retryable)."""

    def finish(self, lease: Lease, *, tokens_used: int, status: str, terminal_reason: str) -> None: ...


class LocalChannel:
    def __init__(
        self, ledger: Ledger, lease_seconds: float = 60, clock=time.time, artifacts: ArtifactStore | None = None
    ):
        self._ledger, self._lease_seconds, self._clock = ledger, lease_seconds, clock
        self.artifacts = artifacts or InMemoryArtifactStore()
        self._intake = ArtifactIntake(ledger, self.artifacts)

    def upload(self, lease: Lease, *, name: str, mime: str, data: bytes) -> ArtifactRef:
        return self._intake.accept(lease.run_id, token=lease.fencing_token, name=name, mime=mime, data=data)

    def claim(self, worker_id: str) -> Lease | None:
        return self._ledger.claim(worker_id, self._lease_seconds, self._clock())

    def heartbeat(self, lease: Lease) -> bool:
        return self._ledger.heartbeat(lease.run_id, lease.fencing_token, self._lease_seconds, self._clock())

    def report(
        self, lease: Lease, wseq: int, type: str, payload: dict | None = None, status: str | None = None
    ) -> bool:
        return self._ledger.report(lease.run_id, lease.fencing_token, wseq, type, payload, status)

    def finish(self, lease: Lease, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._ledger.finish(
            lease.run_id, lease.fencing_token, tokens_used=tokens_used, status=status, terminal_reason=terminal_reason
        )


_CONFLICTS = {"fencing": FencingError, "sequence_gap": SequenceGap}


class HttpChannel:
    """Bearer-authenticated JSON over urllib. Error text never includes bodies or the token."""

    def __init__(
        self,
        base_url: str,
        token: Secret,
        timeout: float = 30,
        *,
        allow_private: bool = False,
        policy: EgressPolicy | None = None,
    ):
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be http(s)://host[:port][/path]")
        self._base = base_url.rstrip("/")
        self._token, self._timeout = token, timeout
        self._allow_loopback = is_loopback_host(parsed.hostname)
        self._allow_private = allow_private
        self._policy = policy or EgressPolicy()

    def _post(self, path: str, body: dict) -> bytes | None:
        return self._send(path, json.dumps(body).encode(), {"Content-Type": "application/json"})

    def _send(self, path: str, data: bytes, headers: dict[str, str]) -> bytes | None:
        url = f"{self._base}/api/v1/worker/{path}"
        try:
            self._policy.check(url, allow_loopback=self._allow_loopback, allow_private=self._allow_private)
        except EgressDenied as exc:
            raise ChannelError(str(exc)) from None
        req = urllib.request.Request(  # noqa: S310  URL is operator-configured and passes the egress policy check
            url, data=data, method="POST", headers={**headers, "Authorization": f"Bearer {self._token.reveal()}"}
        )
        try:
            with opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return raw if resp.status != 204 and raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                try:
                    code = json.loads(exc.read())["detail"]["code"]
                except (ValueError, KeyError, TypeError):
                    code = None
                if code in _CONFLICTS:
                    raise _CONFLICTS[code](code) from None
            if exc.code in (400, 413) and "/artifacts" in path:
                try:
                    code = json.loads(exc.read())["detail"]["code"]
                except (ValueError, KeyError, TypeError):
                    code = "rejected"
                raise ArtifactRejected(code if isinstance(code, str) else "rejected") from None
            raise ChannelError(f"http_error (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise ChannelError("connection_error") from None

    def claim(self, worker_id: str) -> Lease | None:
        raw = self._post("claim", {"worker_id": worker_id})
        try:
            return Lease.model_validate_json(raw) if raw else None
        except ValueError:
            raise ChannelError("bad_response") from None

    def heartbeat(self, lease: Lease) -> bool:
        return self._cancel_flag(self._post(f"runs/{lease.run_id}/heartbeat", {"fencing_token": lease.fencing_token}))

    @staticmethod
    def _cancel_flag(raw: bytes | None) -> bool:
        try:
            return bool(raw and json.loads(raw).get("cancel_requested"))
        except (ValueError, AttributeError):
            raise ChannelError("bad_response") from None

    def report(
        self, lease: Lease, wseq: int, type: str, payload: dict | None = None, status: str | None = None
    ) -> bool:
        return self._cancel_flag(
            self._post(
                f"runs/{lease.run_id}/events",
                {
                    "fencing_token": lease.fencing_token,
                    "wseq": wseq,
                    "type": type,
                    "payload": payload or {},
                    "status": status,
                },
            )
        )

    def upload(self, lease: Lease, *, name: str, mime: str, data: bytes) -> ArtifactRef:
        raw = self._send(
            f"runs/{lease.run_id}/artifacts?{urlencode({'name': name, 'mime': mime})}",
            data,
            {
                "Content-Type": "application/octet-stream",
                "X-Fencing-Token": str(lease.fencing_token),
                "X-Content-Digest": sha256_digest(data),
            },
        )
        try:
            return ArtifactRef.model_validate_json(raw or b"")
        except ValueError:
            raise ChannelError("bad_response") from None

    def finish(self, lease: Lease, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._post(
            f"runs/{lease.run_id}/finish",
            {
                "fencing_token": lease.fencing_token,
                "tokens_used": tokens_used,
                "status": status,
                "terminal_reason": terminal_reason,
            },
        )
