"""Egress allowlist proxy (maef_api.egress_proxy) and how the sandbox and worker use it.

No real containers and no real internet. The end-to-end proxy tests fake DNS and the proxy's upstream
connects: names resolve to a public IP, and connects to that IP are served by a local echo server.
Numeric hosts go through the real resolver, which never leaves the machine. The podman CLI is a fake
script on PATH (see test_sandbox_cli.py). 
"""
import asyncio
import json
import socket
from ipaddress import ip_address
from types import SimpleNamespace

import pytest
import yaml
from maef_api import egress_proxy as ep
from maef_api.channel import LocalChannel
from maef_api.definitions import ResolveError
from maef_api.egress_proxy import Policy, classify_address, summarize_log, vet_addresses
from maef_api.ledger import Ledger
from maef_api.lint import lint_repo
from maef_api.sandbox import EGRESS_PROXY_IMAGE, EgressSpec, FakeSandbox, OciCliSandbox
from maef_api.worker import Worker
from pydantic import ValidationError
from test_mcp import (
    ToolCallingAdapter,
    admit_mcp,
    mcp_manifest,
    mcp_pinned,
    spawner,
    write_fixture,
)
from test_mcp import (
    resolve as resolve_tree,
)
from test_sandbox_cli import RUN_ID, calls, make_cli, opt, opts
from test_sandbox_cli import profile as cli_profile

PUBLIC_IP = "93.184.216.34"
_REAL_OPEN = asyncio.open_connection
_REAL_GAI = socket.getaddrinfo
XFAIL_GAP = "known gap in egress_proxy.classify_address (see report)"


@pytest.fixture(autouse=True)
def bindir(tmp_path, monkeypatch):
    """PATH holds only the fakes, so a real runtime on this machine is never found."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


# --- classify_address ---

PUBLIC = ["93.184.216.34", "1.1.1.1", "172.32.0.1", "172.15.0.1", "100.128.0.1", "100.63.255.255",
          "2606:2800:220:1::1", "2a00:1450:4001::200e"]


@pytest.mark.parametrize("ip", PUBLIC)
def test_public_addresses_are_allowed(ip):
    assert classify_address(ip_address(ip)) is None


@pytest.mark.parametrize("ip, reason", [
    ("127.0.0.1", "loopback"), ("127.8.8.8", "loopback"), ("::1", "loopback"),
    ("10.0.0.1", "private_network"), ("172.16.0.5", "private_network"), ("172.31.255.254", "private_network"),
    ("192.168.1.10", "private_network"), ("100.64.0.1", "private_network"),
    ("100.127.255.254", "private_network"), ("100.100.100.200", "private_network"),
    ("fc00::1", "private_network"), ("fd12:3456::1", "private_network"),
    ("169.254.169.254", "forbidden_address"), ("169.254.0.1", "forbidden_address"),
    ("fe80::1", "forbidden_address"), ("fd00:ec2::254", "forbidden_address"),
    ("224.0.0.1", "forbidden_address"), ("ff02::1", "forbidden_address"),
    ("240.0.0.1", "forbidden_address"), ("255.255.255.255", "forbidden_address"),
    ("0.0.0.0", "forbidden_address"), ("0.1.2.3", "forbidden_address"), ("::", "forbidden_address"),
    ("100::1", "forbidden_address"),
    ("::ffff:10.0.0.1", "private_network"), ("::ffff:100.64.0.1", "private_network"),
    ("::ffff:169.254.169.254", "forbidden_address"), ("::ffff:127.0.0.1", "loopback"),
    ("::a9fe:a9fe", "forbidden_address"),
    ("64:ff9b::a9fe:a9fe", "forbidden_address"), ("64:ff9b::a00:1", "forbidden_address"),
])
def test_denied_addresses_have_a_reason(ip, reason):
    assert classify_address(ip_address(ip)) == reason


@pytest.mark.parametrize("ip", ["169.254.169.254", "fd00:ec2::254", "::ffff:169.254.169.254", "0.0.0.0", "224.0.0.1"])
def test_forbidden_addresses_stay_denied_with_every_flag(ip):
    assert classify_address(ip_address(ip), allow_loopback=True, allow_private=True) == "forbidden_address"


def test_loopback_and_private_are_gated_by_flags():
    assert classify_address(ip_address("127.0.0.1"), allow_loopback=True) is None
    assert classify_address(ip_address("10.1.2.3"), allow_private=True) is None
    assert classify_address(ip_address("100.64.0.1"), allow_loopback=True) == "private_network"


@pytest.mark.parametrize("ip", [
    "2002:0a00:0001::1",
    "2002:a9fe:a9fe::1",
    "fec0::1",
    "198.18.0.1",
    "192.0.2.1",
    "2001:db8::1",
])
def test_non_public_ranges_that_are_not_denied_yet(ip):
    assert classify_address(ip_address(ip)) is not None


# --- Policy: host and port rules ---

@pytest.mark.parametrize("pattern, host", [
    ("example.com", "example.com"),
    ("example.com", "EXAMPLE.com"),
    ("Example.COM", "example.com"),
    ("example.com", "example.com."),
    ("*.example.com", "a.example.com"),
    ("*.example.com", "a.b.example.com"),
    ("*.example.com", "A.Example.Com."),
    ("*", "docs.python.org"),
    ("bücher.example", "xn--bcher-kva.example"),
    ("xn--bcher-kva.example", "BÜCHER.example"),
    ("bücher.example", "bücher.example."),
])
def test_allowed_host_matches(pattern, host):
    assert Policy([pattern]).deny_reason(host, 443) is None


@pytest.mark.parametrize("pattern, host", [
    ("example.com", "www.example.com"),
    ("example.com", "example.com.evil.net"),
    ("*.example.com", "example.com"),
    ("*.example.com", "evil-example.com"),
    ("*.example.com", "xexample.com"),
    ("*.example.com", "example.com.evil.net"),
])
def test_host_not_matching_pattern_is_denied(pattern, host):
    assert Policy([pattern]).deny_reason(host, 443) == "host_not_allowed"


@pytest.mark.parametrize("host", ["exa mple.com", "a/b.com", "", "a" * 64 + ".example.com", "a..b.com"])
def test_malformed_host_is_invalid(host):
    assert Policy(["*"]).deny_reason(host, 443) == "invalid_host"


def test_ports_outside_the_allowlist_are_denied_before_the_host_is_checked():
    assert Policy(["example.com"]).deny_reason("example.com", 8080) == "port_not_allowed"
    assert Policy(["*"], ports=(443,)).deny_reason("docs.python.org", 80) == "port_not_allowed"
    assert Policy(["example.com"], ports=(443, 8080)).deny_reason("example.com", 8080) is None


def test_invalid_patterns_are_skipped_by_policy():
    assert Policy(["*.", "http://x", "a*b.com"]).deny_reason("x", 443) == "host_not_allowed"


# --- vet_addresses ---

def test_any_forbidden_address_denies_the_whole_resolution():
    assert vet_addresses([PUBLIC_IP, "10.0.0.7"]) == "private_network"
    assert vet_addresses(["10.0.0.7", PUBLIC_IP]) == "private_network"
    assert vet_addresses([PUBLIC_IP, "::1"]) == "loopback"


def test_all_public_addresses_pass():
    assert vet_addresses([PUBLIC_IP, "2606:2800:220:1::1"]) is None


@pytest.mark.parametrize("addresses", [[], None, ["not-an-ip"]])
def test_empty_or_unparseable_resolution_is_unresolvable(addresses):
    assert vet_addresses(addresses) == "unresolvable"


# --- the asyncio proxy, end to end on 127.0.0.1 ---

@pytest.fixture
def net(monkeypatch):
    """Fake DNS and upstream connects. Names in `dns` resolve to the listed IPs (and are recorded in
    `lookups`); connects to PUBLIC_IP are served by the local upstream on `upstream_port`; any other
    proxy-side connect fails, so nothing reaches a real network."""
    st = SimpleNamespace(
        dns={"example.com": [PUBLIC_IP], "xn--bcher-kva.example": [PUBLIC_IP], "evil.example": [PUBLIC_IP],
             "mixed.example": [PUBLIC_IP, "10.0.0.1"]},
        lookups=[], connects=[], upstream_port=None)

    def gai(host, port, family=0, type=0, proto=0, flags=0):
        if host in st.dns:
            st.lookups.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in st.dns[host]]
        return _REAL_GAI(host, port, family, type, proto, flags)

    async def open_conn(host, port, **kw):
        st.connects.append((host, port))
        if host == PUBLIC_IP:
            return await _REAL_OPEN("127.0.0.1", st.upstream_port)
        raise OSError("test: no network")

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(asyncio, "open_connection", open_conn)
    return st


async def serve_upstream(seen):
    async def echo(reader, writer):
        while data := await reader.read(65536):
            seen.append(data)
            writer.write(data)
            await writer.drain()
        writer.close()
    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def serve_proxy(hosts, *, ports=ep.DEFAULT_PORTS, **kw):
    kw.setdefault("max_connections", 32)
    proxy = ep._Proxy(Policy(hosts, ports), idle_seconds=5, max_bytes=1 << 20, **kw)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=ep.MAX_HEADER_BYTES)
    return server, server.sockets[0].getsockname()[1]


async def dial(port):
    return await _REAL_OPEN("127.0.0.1", port)


async def head_of(reader):
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)


async def connect_through(port, target):
    r, w = await dial(port)
    w.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await w.drain()
    return r, w, (await head_of(r)).split(b"\r\n")[0].decode()


async def logs_after(capsys, acc, count):
    """Waits until `count` raw log lines were printed; `acc` keeps the raw lines across calls."""
    for _ in range(250):
        acc.extend(line for line in capsys.readouterr().out.splitlines() if line)
        if len(acc) >= count:
            return [json.loads(line) for line in acc]
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {count} log lines, got {acc}")


def denied(net, capsys, hosts, target):
    async def main():
        _, port = await serve_proxy(hosts)
        _, _, status = await connect_through(port, target)
        return status, (await logs_after(capsys, [], 1))[0]
    return asyncio.run(main())


def test_connect_tunnels_bytes_both_ways_to_the_vetted_address(net, capsys):
    async def main():
        seen = []
        _, net.upstream_port = await serve_upstream(seen)
        _, port = await serve_proxy(["example.com"])
        r, w, status = await connect_through(port, "example.com:443")
        assert status == "HTTP/1.1 200 Connection Established"
        w.write(b"ping")
        await w.drain()
        assert await asyncio.wait_for(r.readexactly(4), 5) == b"ping"
        w.close()
        records = await logs_after(capsys, [], 1)
        return seen, records

    seen, records = asyncio.run(main())
    assert seen == [b"ping"]
    assert net.lookups == ["example.com"]
    assert net.connects == [(PUBLIC_IP, 443)]  # the vetted IP, never the name
    assert records[0]["decision"] == "allow" and records[0]["bytes"] == 8


def test_disallowed_host_is_403_and_is_never_resolved_or_connected(net, capsys):
    status, record = denied(net, capsys, ["example.com"], "evil.example:443")
    assert status == "HTTP/1.1 403 Forbidden"
    assert (record["decision"], record["reason"]) == ("deny", "host_not_allowed")
    assert net.lookups == [] and net.connects == []


def test_disallowed_port_is_403_and_is_never_resolved(net, capsys):
    status, record = denied(net, capsys, ["example.com"], "example.com:8443")
    assert status == "HTTP/1.1 403 Forbidden"
    assert record["reason"] == "port_not_allowed"
    assert net.lookups == [] and net.connects == []


@pytest.mark.parametrize("target, reason", [
    ("10.0.0.1:443", "private_network"),
    ("127.0.0.1:443", "loopback"),
    ("[::1]:443", "loopback"),
    ("[::ffff:10.0.0.1]:443", "private_network"),
    ("[fd00:ec2::254]:443", "forbidden_address"),
    ("169.254.169.254:443", "forbidden_address"),
    ("100.100.100.200:443", "private_network"),
    ("0:443", "forbidden_address"),
    ("2130706433:443", "loopback"),  # decimal IPv4
    ("0x7f000001:443", "loopback"),  # hex IPv4
    ("0177.0.0.1:443", "loopback"),  # octal IPv4
])
def test_ip_literal_targets_are_vetted_as_addresses(net, capsys, target, reason):
    # `*` lets every name through the policy; the literal's address must still be denied.
    status, record = denied(net, capsys, ["*"], target)
    assert status == "HTTP/1.1 403 Forbidden"
    assert record["reason"] == reason
    assert net.connects == []


def test_name_resolving_to_one_private_address_is_denied(net, capsys):
    status, record = denied(net, capsys, ["mixed.example"], "mixed.example:443")
    assert status == "HTTP/1.1 403 Forbidden" and record["reason"] == "private_network"
    assert net.connects == []


def test_plain_http_is_forwarded_in_origin_form_without_hop_headers(net, capsys):
    async def main():
        seen = []
        _, net.upstream_port = await serve_upstream(seen)
        _, port = await serve_proxy(["example.com"])
        r, w = await dial(port)
        w.write(b"GET http://example.com/a/b?q=1 HTTP/1.1\r\nHost: example.com\r\n"
                b"Proxy-Connection: keep-alive\r\nX-Keep: yes\r\n\r\n")
        w.write_eof()
        await asyncio.wait_for(r.read(), 5)  # the echo closes once the upstream side ends
        return seen, await logs_after(capsys, [], 1)

    seen, records = asyncio.run(main())
    head = b"".join(seen)
    assert head.startswith(b"GET /a/b?q=1 HTTP/1.1\r\n")
    assert b"Connection: close" in head and b"X-Keep: yes" in head
    assert b"Proxy-Connection" not in head
    assert net.connects == [(PUBLIC_IP, 80)]
    assert records[0]["port"] == 80 and records[0]["decision"] == "allow"


def test_plain_http_is_port_80_only_even_when_the_port_is_allowed(net, capsys):
    # Surprising but intentional in the source: plain HTTP ignores allowed_ports and only ever uses 80.
    async def main():
        _, port = await serve_proxy(["example.com"], ports=(443, 80, 8080))
        r, w = await dial(port)
        w.write(b"GET http://example.com:8080/ HTTP/1.1\r\nHost: example.com:8080\r\n\r\n")
        return (await head_of(r)).split(b"\r\n")[0]

    assert asyncio.run(main()) == b"HTTP/1.1 403 Forbidden"
    assert net.connects == []


@pytest.mark.parametrize("raw, status", [
    (b"NONSENSE\r\n\r\n", b"HTTP/1.1 400"),
    (b"CONNECT example.com HTTP/1.1\r\n\r\n", b"HTTP/1.1 400"),  # no port
    (b"CONNECT example.com:443 HTTP/2\r\n\r\n", b"HTTP/1.1 400"),
    (b"GET http://user@example.com/ HTTP/1.1\r\n\r\n", b"HTTP/1.1 400"),
    (b"GET http://example.com/ HTTP/1.1\r\nContent-Length: 5\r\n\r\n", b"HTTP/1.1 400"),
    (b"GET / HTTP/1.1\r\n\r\n", b"HTTP/1.1 405"),  # origin-form is not a proxy request
    (b"POST http://example.com/ HTTP/1.1\r\n\r\n", b"HTTP/1.1 405"),
    (b"GET http://example.com:8080/ HTTP/1.1\r\n\r\n", b"HTTP/1.1 403"),
])
def test_malformed_or_unsupported_requests_get_an_error_status(net, raw, status):
    async def main():
        _, port = await serve_proxy(["example.com"])
        r, w = await dial(port)
        w.write(raw)
        await w.drain()
        return (await head_of(r)).split(b"\r\n")[0]

    assert asyncio.run(main()).startswith(status)
    assert net.connects == [] and net.lookups == []


def test_clients_outside_the_internal_subnet_are_dropped_silently(net):
    async def main():
        _, port = await serve_proxy(["example.com"], client_cidrs=["10.200.0.0/16"])
        r, w = await dial(port)
        w.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await w.drain()
        return await asyncio.wait_for(r.read(), 5)

    try:  # the proxy closes with the request unread, so the peer may see a reset rather than EOF
        assert asyncio.run(main()) == b""
    except ConnectionResetError:
        pass
    assert net.lookups == [] and net.connects == []


def test_client_inside_the_subnet_is_served(net):
    async def main():
        _, net.upstream_port = await serve_upstream([])
        _, port = await serve_proxy(["example.com"], client_cidrs=["127.0.0.0/8"])
        _, _, status = await connect_through(port, "example.com:443")
        return status

    assert asyncio.run(main()) == "HTTP/1.1 200 Connection Established"


def test_connection_limit_answers_503_busy(net, capsys):
    async def main():
        _, net.upstream_port = await serve_upstream([])
        _, port = await serve_proxy(["example.com"], max_connections=1)
        _r1, w1, first = await connect_through(port, "example.com:443")
        r2, w2 = await dial(port)
        w2.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        second = (await head_of(r2)).split(b"\r\n")[0]
        record = (await logs_after(capsys, [], 1))[0]
        w1.close()
        w2.close()
        return first, second, record

    first, second, record = asyncio.run(main())
    assert first == "HTTP/1.1 200 Connection Established"
    assert second == b"HTTP/1.1 503 Service Unavailable"
    assert record["reason"] == "too_many_connections"


def test_log_lines_hold_host_port_decision_and_bytes_only(net, capsys):
    async def main():
        _, net.upstream_port = await serve_upstream([])
        _, port = await serve_proxy(["example.com"])
        acc = []
        r, w = await dial(port)  # allowed plain HTTP with a path, query and header marker
        w.write(b"GET http://example.com/SECRET-PATH-7d3f?k=SECRET-QUERY-55 HTTP/1.1\r\nHost: example.com\r\n"
                b"X-Token: HDR-SECRET-91\r\n\r\n")
        w.write_eof()
        await asyncio.wait_for(r.read(), 5)
        r2, w2, _ = await connect_through(port, "example.com:443")  # tunnel carrying a body marker
        w2.write(b"BODY-SECRET-33")
        await w2.drain()
        await asyncio.wait_for(r2.readexactly(len(b"BODY-SECRET-33")), 5)
        w2.close()
        r3, w3 = await dial(port)  # denied request whose path carries a marker
        w3.write(b"GET http://evil.example/SECRET-DENY-PATH HTTP/1.1\r\nHost: evil.example\r\n\r\n")
        await w3.drain()
        await head_of(r3)
        records = await logs_after(capsys, acc, 3)
        return acc, records

    raw, records = asyncio.run(main())
    text = "\n".join(raw)
    for marker in ("SECRET-PATH", "SECRET-QUERY", "HDR-SECRET", "BODY-SECRET", "SECRET-DENY-PATH"):
        assert marker not in text
    for rec in records:
        assert set(rec) == {"event", "ts", "host", "port", "decision", "reason", "bytes"}
        assert rec["event"] == "decision"
    # the two allows finish in either order; only the set of decisions is fixed
    assert sorted((r["decision"], r["host"]) for r in records) == [
        ("allow", "example.com"), ("allow", "example.com"), ("deny", "evil.example")]


# --- summarize_log ---

def test_summarize_counts_decisions_and_hosts_and_ignores_junk():
    lines = [
        '{"event":"ready","bind":"0.0.0.0"}',
        '{"event":"decision","ts":1,"host":"example.com","port":443,"decision":"allow","reason":"ok","bytes":100}',
        '{"event":"decision","ts":2,"host":"evil.com","port":443,"decision":"deny","reason":"host_not_allowed","bytes":0}',
        '{"event":"decision","ts":3,"host":"example.com","port":80,"decision":"deny","reason":"port_not_allowed","bytes":0}',
        '{"event":"decision","ts":4,"host":"example.com","port":443,"decision":"error","reason":"connect_failed","bytes":0}',
        "not json at all",
        "[1, 2, 3]",
        '{"event":"decision","decision":"weird","host":"x.com","bytes":5}',
        '{"event":"decision","decision":"allow","host":5,"bytes":5}',
        '{"event":"decision","decision":"allow","host":"example.com","bytes":"12"}',
    ]
    summary = summarize_log("\n".join(lines))
    assert summary["decisions"] == {"allow": 2, "deny": 2, "error": 1}
    assert summary["hosts"] == {"example.com": {"allow": 2, "deny": 1, "error": 1}, "evil.com": {"deny": 1}}
    assert summary["bytes"] == 100


def test_summarize_caps_the_host_table_but_not_the_counts():
    text = "\n".join(f'{{"event":"decision","host":"h{i}.example","decision":"deny","bytes":0}}'
                     for i in range(ep.MAX_LOG_HOSTS + 50))
    summary = summarize_log(text)
    assert len(summary["hosts"]) == ep.MAX_LOG_HOSTS
    assert summary["decisions"] == {"deny": ep.MAX_LOG_HOSTS + 50}


def test_summarize_of_nothing_is_empty():
    assert summarize_log("") == {"decisions": {}, "hosts": {}, "bytes": 0}


# --- manifest validation ---

def test_allowlist_manifest_with_hosts_and_ports_is_valid():
    m = mcp_manifest(network="allowlist", allowed_hosts=["example.com", "*.example.org", "*"], allowed_ports=[443])
    assert m.allowed_ports == [443]


@pytest.mark.parametrize("overrides", [
    {"network": "allowlist"},  # no hosts
    {"network": "allowlist", "allowed_hosts": ["example.com"], "allowed_ports": []},
    {"network": "allowlist", "allowed_hosts": ["example.com"], "allowed_ports": [0]},
    {"network": "allowlist", "allowed_hosts": ["example.com"], "allowed_ports": [65536]},
    {"network": "none", "allowed_hosts": ["example.com"]},
    {"network": "open", "allowed_hosts": ["example.com"]},
    {"network": "open", "allowed_ports": [8080]},
    {"network": "none", "allowed_ports": [8080]},
])
def test_hosts_and_ports_follow_the_network_mode(overrides):
    with pytest.raises(ValidationError):
        mcp_manifest(**overrides)


def test_no_network_manifest_with_default_ports_is_valid():
    assert mcp_manifest(network="none").allowed_hosts == []


@pytest.mark.parametrize("entry", ["*.", "http://x", "example.com/path", "exa mple.com", "x@y.com", "a.example.com?x"])
def test_invalid_host_patterns_are_rejected(entry):
    with pytest.raises(ValidationError):
        mcp_manifest(network="allowlist", allowed_hosts=[entry])


@pytest.mark.parametrize("entry", [
    "a*b.com",
    "**",
    "*.*.com",
    "*x.com",
])
def test_wildcards_only_in_the_documented_forms(entry):
    with pytest.raises(ValidationError):
        mcp_manifest(network="allowlist", allowed_hosts=[entry])


def test_allowlist_needs_agent_network_open(tmp_path):
    paths = write_fixture(tmp_path, network_open=False)
    (paths[2] / "fetch.yaml").write_text(yaml.safe_dump(
        mcp_manifest(network="allowlist", allowed_hosts=["example.com"]).model_dump(mode="json")))
    with pytest.raises(ResolveError, match="network_open"):
        resolve_tree(paths)


def test_allowlist_resolves_when_agent_network_open_is_set(tmp_path):
    paths = write_fixture(tmp_path, network_open=True)
    (paths[2] / "fetch.yaml").write_text(yaml.safe_dump(
        mcp_manifest(network="allowlist", allowed_hosts=["example.com"]).model_dump(mode="json")))
    assert resolve_tree(paths).mcp_servers[0].network == "allowlist"


def test_lint_reports_allowlist_without_network_open(tmp_path):
    paths = write_fixture(tmp_path, network_open=False)
    (paths[2] / "fetch.yaml").write_text(yaml.safe_dump(
        mcp_manifest(network="allowlist", allowed_hosts=["example.com"]).model_dump(mode="json")))
    root = tmp_path
    findings = lint_repo(root)
    assert any(f.severity == "error" and "network_open" in f.message for f in findings), findings


def test_lint_is_clean_on_network_for_allowlist_with_network_open(tmp_path):
    paths = write_fixture(tmp_path, network_open=True)
    (paths[2] / "fetch.yaml").write_text(yaml.safe_dump(
        mcp_manifest(network="allowlist", allowed_hosts=["example.com"]).model_dump(mode="json")))
    findings = lint_repo(tmp_path)
    assert not [f for f in findings if f.severity == "error" and "network" in f.message], findings


# --- worker with FakeSandbox ---

class EgressSandbox(FakeSandbox):
    """A FakeSandbox whose sessions report a proxy decision summary, as an allowlist session would."""

    def provision(self, *args, **kwargs):
        session = super().provision(*args, **kwargs)
        session.egress = {"decisions": {"allow": 2, "deny": 1},
                          "hosts": {"example.com": {"allow": 2}, "evil.example": {"deny": 1}}, "bytes": 512}
        return session


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / 'egress.db')
    s.ensure_account('default', 1_000_000)
    return s


def test_allowlist_server_events_and_egress_summary(store):
    manifest = mcp_manifest(network="allowlist", allowed_hosts=["example.com"])
    run_id = admit_mcp(store, mcp_pinned(manifest))
    made, adapter = [], ToolCallingAdapter("mcp__fetch__fetch")
    sandbox_ = EgressSandbox(spawn_factory=spawner(made))
    assert Worker(LocalChannel(store), lambda p: adapter, worker_id="w1", sandbox=sandbox_).run_once() is True

    detail = store.get(run_id)
    assert detail.status == "succeeded"
    assert sandbox_.egress_specs == [EgressSpec(("example.com",), (443, 80))]
    assert sandbox_.provisioned[0][2] == "allowlist"
    types = [e.type for e in detail.events]
    assert types.index("mcp.started") < types.index("egress.proxy_started") < types.index("tool.call")
    assert types.index("mcp.stopped") < types.index("egress.summary") < types.index("task.succeeded")

    started = next(e.payload for e in detail.events if e.type == "egress.proxy_started")
    assert started == {"server": "fetch", "allowed_hosts": 1, "any_public_host": False, "ports": [443, 80]}
    summary = next(e.payload for e in detail.events if e.type == "egress.summary")
    assert set(summary) == {"server", "decisions", "hosts", "bytes"}
    assert summary["server"] == "fetch" and summary["decisions"] == {"allow": 2, "deny": 1}
    assert summary["hosts"] == {"example.com": {"allow": 2}, "evil.example": {"deny": 1}}
    assert made[0].closed


# --- podman CLI calls (fake podman on PATH) ---

PODMAN_HEAD = ('case "$*" in\n'
               '  --version*) echo "podman version 5.2.1";;\n'
               '  *Rootless*) echo true;;\n'
               '  info*) echo true;;\n')
PROXY_IP = "10.89.7.2"
SUBNET = "10.89.7.0/24"


def podman(tmp_path, extra=""):
    """Fake podman: answers the subcommands the egress path uses and logs every call."""
    make_cli(tmp_path, "podman", PODMAN_HEAD + extra + "\nesac")
    return OciCliSandbox("podman", str(tmp_path / "bin" / "podman"), env={})


EGRESS_CASES = (
    f"  network\\ inspect*) echo {SUBNET};;\n"
    "  logs*) echo '{\"event\": \"ready\", \"bind\": \"0.0.0.0\"}';;\n"
    f"  inspect*) echo {PROXY_IP};;\n"
)


def provision_allowlist(tmp_path):
    sb = podman(tmp_path, EGRESS_CASES)
    session = sb.provision(cli_profile(), RUN_ID, network="allowlist",
                           egress=EgressSpec(("example.com",), (443, 80)))
    return session, calls(tmp_path, "podman")


def first(cl, pred):
    return next(c for c in cl if pred(c))


def test_allowlist_provisioning_builds_internal_network_and_hardened_proxy(tmp_path):
    _session, cl = provision_allowlist(tmp_path)
    create = first(cl, lambda c: c[:2] == ["network", "create"])
    assert "--internal" in create and "--disable-dns" in create
    assert opt(create, "--label") == f"maef.run={RUN_ID}"
    net = create[-1]
    assert net.startswith("maef-egress-")

    proxy_run = first(cl, lambda c: c[0] == "run" and EGRESS_PROXY_IMAGE in c)
    assert proxy_run[proxy_run.index("--name") + 1].startswith("maef-proxy-")
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert flag in proxy_run
    assert opt(proxy_run, "--user") == "65534:65534"
    assert opt(proxy_run, "--network") == "podman"  # the default network: the way out
    assert "-u" in proxy_run and "-c" in proxy_run  # the proxy source is passed inline with -c
    config = json.loads(next(e for e in opts(proxy_run, "--env") if e.startswith("MAEF_EGRESS_CONFIG="))
                        .split("=", 1)[1])
    assert config["hosts"] == ["example.com"] and config["ports"] == [443, 80]
    assert config["client_cidrs"] == [SUBNET]

    connect = first(cl, lambda c: c[:2] == ["network", "connect"])
    assert connect[2] == net and connect[3] == opt(proxy_run, "--name")


def test_allowlist_mcp_container_gets_only_the_internal_network_and_proxy_env(tmp_path):
    _session, cl = provision_allowlist(tmp_path)
    net = first(cl, lambda c: c[:2] == ["network", "create"])[-1]
    mcp_run = first(cl, lambda c: bool(c) and c[0] == "run" and EGRESS_PROXY_IMAGE not in c)
    assert f"--network={net}" in mcp_run
    assert not any(a.startswith("--network=") and a != f"--network={net}" for a in mcp_run)
    assert "--network=none" not in mcp_run and "-p" not in mcp_run and "--publish" not in mcp_run
    env = dict(e.split("=", 1) for e in opts(mcp_run, "--env"))
    url = f"http://{PROXY_IP}:{ep.PROXY_PORT}"
    assert {k: env[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")} == {
        "HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url}
    # NO_PROXY is set empty on purpose (the MCP container must not bypass the proxy).
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""


def test_teardown_removes_proxy_mcp_container_and_network(tmp_path):
    session, _ = provision_allowlist(tmp_path)
    session.teardown()
    cl = calls(tmp_path, "podman")
    proxy = session._proxy
    assert ["rm", "-f", "-t", "0", proxy] in cl
    assert ["rm", "-f", "-t", "0", session.name] in cl
    assert ["network", "rm", "-f", session._network] in cl
    assert session._network.startswith("maef-egress-") and proxy.startswith("maef-proxy-")


def test_cleanup_orphans_removes_labelled_networks_and_containers(tmp_path):
    sb = podman(tmp_path, '  ps\\ -a*) echo cid-orphan;;\n  network\\ ls*) echo maef-egress-old-1;;\n')
    assert sb.cleanup_orphans() == 1
    cl = calls(tmp_path, "podman")
    assert first(cl, lambda c: c[:4] == ["ps", "-a", "-q", "--filter"])[-1] == "label=maef.run"
    assert ["rm", "-f", "-t", "0", "cid-orphan"] in cl
    assert ["network", "rm", "-f", "maef-egress-old-1"] in cl
