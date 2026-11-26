import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import ValidationError

from maef_api.adapters.base import Message
from maef_api.adapters.http import ModelHTTPError, post_json
from maef_api.adapters.openai_compatible import OpenAICompatibleAdapter
from maef_api.credentials import Credentials
from maef_api.egress import EgressDenied, EgressPolicy
from test_http_adapters import Fake, make_profile

URL = "https://api.example.com/secret-path"


@pytest.fixture
def fake():
    made = []

    def make(replies):
        f = Fake(replies)
        made.append(f)
        return f

    yield make
    [f.close() for f in made]


def resolver_for(*ips):
    def resolve(host, port, type=0):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return resolve


def check(ip, *, loopback=False, private=False, url=URL):
    EgressPolicy(resolver=resolver_for(ip)).check(url, allow_loopback=loopback, allow_private=private)


@pytest.mark.parametrize(
    "ip", ["169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1", "fd00:ec2::254", "::ffff:169.254.169.254", "240.0.0.1"]
)
def test_always_denied_even_when_allowed(ip):
    with pytest.raises(EgressDenied):
        check(ip, loopback=True, private=True)


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1"])
def test_loopback_gated_by_flag(ip):
    with pytest.raises(EgressDenied):
        check(ip, loopback=False)
    check(ip, loopback=True)


@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.0.5", "192.168.1.10", "100.64.0.1", "fd12::1"])
def test_private_gated_by_flag(ip):
    with pytest.raises(EgressDenied):
        check(ip, private=False)
    check(ip, private=True)


def test_public_address_allowed_by_default():
    check("93.184.216.34")


def test_any_private_address_denies_hostname():
    policy = EgressPolicy(resolver=resolver_for("93.184.216.34", "10.0.0.7"))
    with pytest.raises(EgressDenied):
        policy.check(URL, allow_loopback=False, allow_private=False)


def test_resolver_error_or_empty_denies():
    def boom(host, port, type=0):
        raise OSError("no such host")

    with pytest.raises(EgressDenied):
        EgressPolicy(resolver=boom).check(URL, allow_loopback=True, allow_private=True)
    with pytest.raises(EgressDenied):
        EgressPolicy(resolver=resolver_for()).check(URL, allow_loopback=True, allow_private=True)


def test_denial_message_omits_path():
    with pytest.raises(EgressDenied) as e:
        check("10.0.0.1", url=URL)
    assert "/secret-path" not in str(e.value)


@pytest.mark.parametrize("url", ["http://example.com/v1", "http://10.0.0.5:8000/v1"])
def test_profile_rejects_plain_http_to_remote_host(url):
    with pytest.raises(ValidationError):
        make_profile("openai-compatible", url)


def test_profile_accepts_plain_http_with_private_opt_in_and_localhost():
    make_profile("openai-compatible", "http://10.0.0.5:8000/v1", extra_endpoint=", allow_private_network: true")
    make_profile("openai-compatible", "http://localhost:1234/v1")


def test_https_public_accepted():
    make_profile("openai-compatible", "https://api.openai.com/v1")


def test_redirect_is_not_followed():
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{server.server_port}/second")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ModelHTTPError):
            post_json(
                f"http://127.0.0.1:{server.server_port}/first",
                {},
                {"x": 1},
                timeout=5,
                max_retries=0,
                policy=EgressPolicy(),
                allow_loopback=True,
                allow_private=False,
            )
    finally:
        server.shutdown()
    assert hits == ["/first"]


def test_adapter_denies_metadata_resolution_before_request(fake):
    srv = fake([])
    p = make_profile("openai-compatible", srv.url + "/v1")
    policy = EgressPolicy(resolver=resolver_for("169.254.169.254"))
    with pytest.raises(ModelHTTPError, match="egress_denied"):
        OpenAICompatibleAdapter(p, Credentials({}), policy=policy).converse(
            system="s", messages=[Message("user", "q")], tools=[], max_output_tokens=5, timeout_seconds=5
        )
    assert srv.requests == []
