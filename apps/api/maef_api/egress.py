"""SSRF guard for outbound model calls."""
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import urlparse

_PRIVATE = [ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")]
_IMDS_V6 = ip_address("fd00:ec2::254")  # AWS IMDS over IPv6; sits inside fc00::/7, so denied explicitly
_THIS_NETWORK = ip_network("0.0.0.0/8")


class EgressDenied(Exception):
    """Never includes the URL: it may carry a path the operator considers sensitive."""


def _embedded(ip: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    """Evaluate the v4 address inside IPv4-mapped (::ffff:a.b.c.d) and IPv4-compatible (::a.b.c.d) forms."""
    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if int(ip) >> 32 == 0 and int(ip) > 1:
            return IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


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
            infos = self._resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addrs = [ip_address(info[4][0].split("%")[0]) for info in infos]
        except (OSError, ValueError):
            raise EgressDenied("egress_denied:unresolvable") from None
        if not addrs:
            raise EgressDenied("egress_denied:unresolvable")
        for raw in addrs:
            ip = _embedded(raw)
            # Loopback first: Python marks ::1 as reserved (it sits in ::/8).
            if ip.is_loopback:
                if not allow_loopback:
                    raise EgressDenied("egress_denied:loopback")
                continue
            if (ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local
                    or ip == _IMDS_V6 or (isinstance(ip, IPv4Address) and ip in _THIS_NETWORK)):
                raise EgressDenied("egress_denied:forbidden_address")
            if not allow_private and any(ip in net for net in _PRIVATE if net.version == ip.version):
                raise EgressDenied("egress_denied:private_network")
