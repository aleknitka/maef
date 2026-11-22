"""WorkerChannel: how a worker talks to the control plane. One interface, two adapters.

LocalChannel calls the Ledger directly (tests, single-host). HttpChannel speaks to the
`/api/v1/worker/` routes with urllib, so a worker needs nothing but outbound HTTPS.
"""
import json
import time
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlparse

from .adapters.http import opener
from .credentials import Secret
from .egress import EgressDenied, EgressPolicy
from .leases import ChannelError, FencingError, Lease, SequenceGap
from .ledger import Ledger
from .models import is_loopback_host


class WorkerChannel(Protocol):
    def claim(self, worker_id: str) -> Lease | None: ...

    def heartbeat(self, lease: Lease) -> None:
        """Raises FencingError when the lease is no longer this worker's."""

    def report(self, lease: Lease, wseq: int, type: str, payload: dict | None = None,
               status: str | None = None) -> bool:
        """Raises FencingError or SequenceGap; False means a duplicate that was ignored."""

    def finish(self, lease: Lease, *, tokens_used: int, status: str, terminal_reason: str) -> None: ...


class LocalChannel:
    def __init__(self, ledger: Ledger, lease_seconds: float = 60, clock=time.time):
        self._ledger, self._lease_seconds, self._clock = ledger, lease_seconds, clock

    def claim(self, worker_id: str) -> Lease | None:
        return self._ledger.claim(worker_id, self._lease_seconds, self._clock())

    def heartbeat(self, lease: Lease) -> None:
        self._ledger.heartbeat(lease.run_id, lease.fencing_token, self._lease_seconds, self._clock())

    def report(self, lease: Lease, wseq: int, type: str, payload: dict | None = None,
               status: str | None = None) -> bool:
        return self._ledger.report(lease.run_id, lease.fencing_token, wseq, type, payload, status)

    def finish(self, lease: Lease, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._ledger.finish(lease.run_id, lease.fencing_token, tokens_used=tokens_used, status=status,
                            terminal_reason=terminal_reason)


_CONFLICTS = {"fencing": FencingError, "sequence_gap": SequenceGap}


class HttpChannel:
    """Bearer-authenticated JSON over urllib. Error text never includes bodies or the token."""

    def __init__(self, base_url: str, token: Secret, timeout: float = 30, *, allow_private: bool = False,
                 policy: EgressPolicy | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("base_url must be http(s)://host[:port][/path]")
        self._base = base_url.rstrip("/")
        self._token, self._timeout = token, timeout
        self._allow_loopback = is_loopback_host(parsed.hostname)
        self._allow_private = allow_private
        self._policy = policy or EgressPolicy()

    def _post(self, path: str, body: dict) -> bytes | None:
        url = f"{self._base}/api/v1/worker/{path}"
        try:
            self._policy.check(url, allow_loopback=self._allow_loopback, allow_private=self._allow_private)
        except EgressDenied as exc:
            raise ChannelError(str(exc)) from None
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self._token.reveal()}"})
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
            raise ChannelError(f"http_error (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise ChannelError("connection_error") from None

    def claim(self, worker_id: str) -> Lease | None:
        raw = self._post("claim", {"worker_id": worker_id})
        try:
            return Lease.model_validate_json(raw) if raw else None
        except ValueError:
            raise ChannelError("bad_response") from None

    def heartbeat(self, lease: Lease) -> None:
        self._post(f"runs/{lease.run_id}/heartbeat", {"fencing_token": lease.fencing_token})

    def report(self, lease: Lease, wseq: int, type: str, payload: dict | None = None,
               status: str | None = None) -> bool:
        raw = self._post(f"runs/{lease.run_id}/events", {
            "fencing_token": lease.fencing_token, "wseq": wseq, "type": type, "payload": payload or {}, "status": status})
        try:
            return bool(raw and json.loads(raw).get("appended"))
        except (ValueError, AttributeError):
            raise ChannelError("bad_response") from None

    def finish(self, lease: Lease, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        self._post(f"runs/{lease.run_id}/finish", {
            "fencing_token": lease.fencing_token, "tokens_used": tokens_used, "status": status,
            "terminal_reason": terminal_reason})
