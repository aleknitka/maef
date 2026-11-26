"""Egress allowlist proxy for MCP servers on `network: allowlist`.

Standalone on purpose: stdlib only and no maef imports, because the worker runs this file's source
with `python -c` in a pinned Python image (nothing is mounted). It is also imported by the API for
the shared address classification (`classify_address`) and the log summary.

Supports `CONNECT host:port` (HTTPS tunnelling) and absolute-form `GET|HEAD http://host/...` (plain
HTTP, port 80, one request per connection). For each request: the port must be allowed, the host must
match the allowlist, the proxy resolves the name itself, every resolved address must be public, and
it then connects to the vetted IP (no second resolution, so DNS rebinding cannot swap the target).
One JSON line per decision goes to stdout: host, port, decision, reason, bytes. Never URLs paths,
headers or bodies. Configuration: JSON in the MAEF_EGRESS_CONFIG environment variable.
"""

import asyncio
import json
import os
import socket
import sys
import time
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

PROXY_PORT = 3128
DEFAULT_PORTS = (443, 80)
DEFAULT_MAX_CONNECTIONS = 32
DEFAULT_IDLE_SECONDS = 60.0
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_LOG_HOSTS = 200

_PRIVATE = [ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")]
_IMDS_V6 = ip_address("fd00:ec2::254")  # AWS IMDS over IPv6; sits inside fc00::/7, so denied explicitly
_THIS_NETWORK = ip_network("0.0.0.0/8")


def _embedded(ip):
    """The v4 address inside IPv4-mapped (::ffff:a.b.c.d) and IPv4-compatible (::a.b.c.d) forms."""
    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if int(ip) >> 32 == 0 and int(ip) > 1:
            return IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def classify_address(ip, *, allow_loopback: bool = False, allow_private: bool = False) -> str | None:
    """None when `ip` is allowed, else a reason: loopback | forbidden_address | private_network.
    Unspecified, multicast, reserved and link-local (cloud metadata) are never allowed."""
    ip = _embedded(ip)
    if ip.is_loopback:  # first: Python marks ::1 as reserved
        return None if allow_loopback else "loopback"
    # Tunnelled IPv4 (6to4 2002::/16, Teredo 2001::/32) is judged by the address it carries.
    inner = getattr(ip, "sixtofour", None) or (ip.teredo[1] if getattr(ip, "teredo", None) else None)
    if inner is not None:
        reason = classify_address(inner, allow_loopback=allow_loopback, allow_private=allow_private)
        if reason:
            return reason
    if (
        ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_link_local
        or getattr(ip, "is_site_local", False)
        or ip == _IMDS_V6
        or (isinstance(ip, IPv4Address) and ip in _THIS_NETWORK)
    ):
        return "forbidden_address"
    if any(ip in net for net in _PRIVATE if net.version == ip.version):
        return None if allow_private else "private_network"
    # Anything else that is not globally routable (documentation, benchmarking, ...) is never a
    # legitimate egress target.
    if not ip.is_global and inner is None:
        return "forbidden_address"
    return None


def normalize_host(host: str) -> str | None:
    """Lower-case, IDNA (punycode) form without a trailing dot, or None when not a plausible host."""
    if not isinstance(host, str):
        return None
    host = host.strip().rstrip(".").lower()
    if not host or len(host) > 253 or any(c in host for c in "/\\@ \t\r\n\0?#*"):
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def normalize_pattern(pattern: str) -> str | None:
    if pattern == "*":
        return "*"
    if pattern.startswith("*."):
        rest = normalize_host(pattern[2:])
        return None if rest is None else "*." + rest
    return normalize_host(pattern)


class Policy:
    """Host and port rules. `*` matches any host (addresses are still vetted); `*.suffix` matches any
    subdomain of suffix but not suffix itself; anything else must match exactly."""

    def __init__(self, hosts, ports=DEFAULT_PORTS):
        self.any_host = False
        self._exact: set[str] = set()
        self._suffix: list[str] = []
        for raw in hosts:
            pattern = normalize_pattern(raw)
            if pattern is None:
                continue
            if pattern == "*":
                self.any_host = True
            elif pattern.startswith("*."):
                self._suffix.append(pattern[1:])  # ".example.com"
            else:
                self._exact.add(pattern)
        self.ports = frozenset(ports)

    def deny_reason(self, host: str, port: int) -> str | None:
        """Checks that need no DNS: port, then host."""
        if port not in self.ports:
            return "port_not_allowed"
        norm = normalize_host(host)
        if norm is None:
            return "invalid_host"
        if self.any_host or norm in self._exact or any(norm.endswith(s) and len(norm) > len(s) for s in self._suffix):
            return None
        return "host_not_allowed"


def vet_addresses(addresses) -> str | None:
    """None when every address is public, else the reason for the first that is not."""
    if not addresses:
        return "unresolvable"
    for raw in addresses:
        try:
            reason = classify_address(ip_address(raw.split("%")[0]))
        except ValueError:
            return "unresolvable"
        if reason:
            return reason
    return None


def summarize_log(text: str) -> dict:
    """Counts per decision and per host from the proxy's stdout. Hosts only; never anything else."""
    decisions: dict[str, int] = {}
    hosts: dict[str, dict[str, int]] = {}
    total_bytes = 0
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("event") != "decision":
            continue
        decision, host = rec.get("decision"), rec.get("host")
        if decision not in ("allow", "deny", "error") or not isinstance(host, str):
            continue
        decisions[decision] = decisions.get(decision, 0) + 1
        total_bytes += rec["bytes"] if isinstance(rec.get("bytes"), int) else 0
        if host in hosts or len(hosts) < MAX_LOG_HOSTS:
            entry = hosts.setdefault(host[:253], {})
            entry[decision] = entry.get(decision, 0) + 1
    return {"decisions": decisions, "hosts": hosts, "bytes": total_bytes}


# --- the server ---


def _log(**fields) -> None:
    print(json.dumps({"event": "decision", "ts": round(time.time(), 3), **fields}, separators=(",", ":")), flush=True)


class _Proxy:
    def __init__(self, policy: Policy, *, max_connections: int, client_cidrs=(), idle_seconds: float, max_bytes: int):
        self.policy, self.idle, self.max_bytes = policy, idle_seconds, max_bytes
        self.slots = asyncio.Semaphore(max_connections)
        self.client_nets = [ip_network(c, strict=False) for c in client_cidrs]
        self.used = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = (writer.get_extra_info("peername") or ("",))[0]
            if self.client_nets and not _in_nets(peer, self.client_nets):
                return  # not a client of the internal network: drop silently
            if self.slots.locked():
                await self._reply(writer, 503, "busy")
                _log(host="-", port=0, decision="deny", reason="too_many_connections", bytes=0)
                return
            async with self.slots:
                await self._serve(reader, writer)
        except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    @staticmethod
    async def _reply(writer, status: int, reason: str) -> None:
        phrase = {
            200: "Connection Established",
            400: "Bad Request",
            403: "Forbidden",
            405: "Method Not Allowed",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }[status]
        body = b"" if status == 200 else reason.encode()
        head = f"HTTP/1.1 {status} {phrase}\r\n"
        if status != 200:
            head += f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
        try:
            writer.write(head.encode() + b"\r\n" + body)
            await writer.drain()
        except OSError:
            pass

    async def _serve(self, reader, writer) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            await self._reply(writer, 400, "bad_request")
            return
        if len(raw) > MAX_HEADER_BYTES:
            await self._reply(writer, 400, "bad_request")
            return
        lines = raw.decode("latin-1").split("\r\n")[:-2]
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            await self._reply(writer, 400, "bad_request")
            return
        method, target = parts[0].upper(), parts[1]
        headers = lines[1:]
        if method == "CONNECT":
            host, port = _split_hostport(target, None)
            if host is None or port is None:
                _log(host="-", port=0, decision="deny", reason="bad_request", bytes=0)
                await self._reply(writer, 400, "bad_request")
                return
            await self._connect(reader, writer, host, port, None)
            return
        if method in ("GET", "HEAD") and target.lower().startswith("http://"):
            rest = target[7:]
            authority, _slash, path = rest.partition("/")
            host, port = _split_hostport(authority, 80)
            if host is None or port is None or "@" in authority:
                _log(host="-", port=0, decision="deny", reason="bad_request", bytes=0)
                await self._reply(writer, 400, "bad_request")
                return
            names = {h.split(":", 1)[0].strip().lower() for h in headers}
            if names & {"content-length", "transfer-encoding"}:
                _log(host=host[:253], port=port, decision="deny", reason="request_body_not_supported", bytes=0)
                await self._reply(writer, 400, "request_body_not_supported")
                return
            if port != 80:
                _log(host=host[:253], port=port, decision="deny", reason="port_not_allowed", bytes=0)
                await self._reply(writer, 403, "port_not_allowed")
                return
            kept = [
                h
                for h in headers
                if h.split(":", 1)[0].strip().lower()
                not in (
                    "connection",
                    "proxy-connection",
                    "proxy-authorization",
                    "keep-alive",
                    "upgrade",
                    "te",
                    "trailer",
                )
            ]
            head = f"{method} /{path} HTTP/1.1\r\n" + "".join(h + "\r\n" for h in kept) + "Connection: close\r\n\r\n"
            await self._connect(reader, writer, host, port, head.encode("latin-1"))
            return
        _log(host="-", port=0, decision="deny", reason="method_not_allowed", bytes=0)
        await self._reply(writer, 405, "method_not_allowed")

    async def _connect(self, reader, writer, host: str, port: int, first: bytes | None) -> None:
        shown = (normalize_host(host) or "<invalid>")[:253]
        reason = self.policy.deny_reason(host, port)
        if reason is None and self.used >= self.max_bytes:
            reason = "byte_budget_exhausted"
        addrs: list[str] = []
        if reason is None:
            try:
                infos = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(normalize_host(host), port, type=socket.SOCK_STREAM), 10
                )
                addrs = list(dict.fromkeys(i[4][0] for i in infos))
            except (TimeoutError, OSError):
                addrs = []
            reason = vet_addresses(addrs)
        if reason is not None:
            _log(host=shown, port=port, decision="deny", reason=reason, bytes=0)
            await self._reply(writer, 403, reason)
            return
        upstream = None
        for addr in addrs:  # connect to the vetted IP itself, never to the name
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(addr, port), 10)
                break
            except (TimeoutError, OSError):
                continue
        if upstream is None:
            _log(host=shown, port=port, decision="error", reason="connect_failed", bytes=0)
            await self._reply(writer, 502, "connect_failed")
            return
        up_reader, up_writer = upstream
        moved = [0]
        try:
            if first is None:
                await self._reply(writer, 200, "")
            else:
                up_writer.write(first)
                moved[0] += len(first)
            await asyncio.gather(self._pipe(reader, up_writer, moved), self._pipe(up_reader, writer, moved))
        except (TimeoutError, OSError, ConnectionError):
            pass
        finally:
            self.used += moved[0]
            _log(host=shown, port=port, decision="allow", reason="ok", bytes=moved[0])
            try:
                up_writer.close()
            except OSError:
                pass

    async def _pipe(self, src, dst, moved: list[int]) -> None:
        try:
            while True:
                data = await asyncio.wait_for(src.read(65536), self.idle)
                if not data:
                    break
                moved[0] += len(data)
                if self.used + moved[0] > self.max_bytes:
                    break
                dst.write(data)
                await dst.drain()
        except (TimeoutError, OSError, ConnectionError):
            pass
        finally:
            try:
                if dst.can_write_eof():
                    dst.write_eof()
                else:
                    dst.close()
            except (OSError, RuntimeError):
                pass


def _in_nets(addr: str, nets) -> bool:
    try:
        ip = ip_address(addr.split("%")[0])
    except ValueError:
        return False
    return any(ip in n for n in nets if n.version == ip.version)


def _split_hostport(text: str, default_port: int | None):
    """(host, port) from `host`, `host:port` or `[v6]:port`; (None, None) when malformed."""
    if not text:
        return None, None
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            return None, None
        port_text = rest[1:] if rest else ""
    elif text.count(":") > 1:
        return None, None
    else:
        host, _, port_text = text.partition(":")
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            return None, None
        return host, int(port_text)
    return (host, default_port) if default_port else (None, None)


async def _main(config: dict) -> None:
    proxy = _Proxy(
        Policy(config.get("hosts", []), config.get("ports", DEFAULT_PORTS)),
        max_connections=int(config.get("max_connections", DEFAULT_MAX_CONNECTIONS)),
        client_cidrs=config.get("client_cidrs", []),
        idle_seconds=float(config.get("idle_seconds", DEFAULT_IDLE_SECONDS)),
        max_bytes=int(config.get("max_bytes", DEFAULT_MAX_BYTES)),
    )
    bind = config.get("bind", "0.0.0.0")  # clients are filtered by source network
    server = await asyncio.start_server(proxy.handle, bind, int(config.get("port", PROXY_PORT)), limit=MAX_HEADER_BYTES)
    print(json.dumps({"event": "ready", "bind": bind}), flush=True)
    async with server:
        await server.serve_forever()


def main() -> int:
    try:
        config = json.loads(os.environ["MAEF_EGRESS_CONFIG"])
    except (KeyError, ValueError):
        print(json.dumps({"event": "fatal", "reason": "bad_config"}), flush=True)
        return 2
    try:
        asyncio.run(_main(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
