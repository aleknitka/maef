"""SSRF guard for outbound model calls."""

import socket
from ipaddress import ip_address
from urllib.parse import urlparse

from .egress_proxy import (
    classify_address,  # one classification for model calls and the MCP proxy
)


class EgressDenied(Exception):
    """Never includes the URL: it may carry a path the operator considers sensitive."""


class EgressPolicy:
    def __init__(self, *, resolver=socket.getaddrinfo):
        self._resolver = resolver

    def check(self, url: str, *, allow_loopback: bool, allow_private: bool) -> None:
        """Deny if ANY resolved address is disallowed. Link-local (cloud metadata) is never allowed.

        Known gap: the name is resolved here and again by the HTTP client at connect time, so a
        DNS-rebinding server can answer differently the second time (TOCTOU). Closing it needs
        connecting to the vetted IP; until then prefer IP-literal or trusted hostnames.
        """
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise EgressDenied("egress_denied:no_host")
        try:
            infos = self._resolver(
                host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
            )
            addrs = [ip_address(str(info[4][0]).split("%")[0]) for info in infos]
        except (OSError, ValueError):
            raise EgressDenied("egress_denied:unresolvable") from None
        if not addrs:
            raise EgressDenied("egress_denied:unresolvable")
        for ip in addrs:
            reason = classify_address(ip, allow_loopback=allow_loopback, allow_private=allow_private)
            if reason:
                raise EgressDenied(f"egress_denied:{reason}")
