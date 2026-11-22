"""Tiny JSON-over-HTTP client with bounded retries. Error text never includes the response
body or request, which can echo prompts or keys."""
import json
import os
import time
import urllib.error
import urllib.request


class ModelHTTPError(Exception):
    def __init__(self, status: int | None, kind: str):
        super().__init__(f"{kind} (HTTP {status})" if status else kind)
        self.status = status


def api_key(credential_ref: str) -> str | None:
    scheme, _, name = credential_ref.partition(":")
    if scheme == "none":
        return None
    if scheme != "env":
        raise ValueError(f"Unsupported credential_ref for an HTTP endpoint: {credential_ref}")
    key = os.environ.get(name)
    if not key:
        raise ModelHTTPError(None, f"missing_credential:{name}")
    return key


def post_json(url: str, headers: dict, body: dict, *, timeout: float, max_retries: int, sleep=time.sleep) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # scheme validated by the profile
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
