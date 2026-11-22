"""Tiny JSON-over-HTTP client with bounded retries. Error text never includes the response
body or request, which can echo prompts or keys."""
import json
import time
import urllib.error
import urllib.request

from ..egress import EgressDenied, EgressPolicy


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would reach a host the egress policy never checked


_opener = urllib.request.build_opener(_NoRedirect)


class ModelHTTPError(Exception):
    def __init__(self, status: int | None, kind: str):
        super().__init__(f"{kind} (HTTP {status})" if status else kind)
        self.status = status


def post_json(url: str, headers: dict, body: dict, *, timeout: float, max_retries: int, policy: EgressPolicy,
              allow_loopback: bool, allow_private: bool, sleep=time.sleep) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(max_retries + 1):
        try:
            policy.check(url, allow_loopback=allow_loopback, allow_private=allow_private)
        except EgressDenied as exc:
            raise ModelHTTPError(None, str(exc)) from None
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
        try:
            with _opener.open(req, timeout=timeout) as resp:  # scheme validated by the profile
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (408, 429) or exc.code >= 500
            if not retryable or attempt == max_retries:
                raise ModelHTTPError(exc.code, "http_error") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == max_retries:
                raise ModelHTTPError(None, "connection_error") from None
        sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")
