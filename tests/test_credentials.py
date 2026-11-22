import pytest
from maef_api.adapters.anthropic import AnthropicAdapter
from maef_api.adapters.base import Message
from maef_api.adapters.openai_compatible import OpenAICompatibleAdapter
from maef_api.credentials import CredentialError, Credentials, Secret
from test_http_adapters import OPENAI_OK, Fake, make_profile


@pytest.fixture
def fake():
    made = []
    def make(replies):
        f = Fake(replies); made.append(f); return f
    yield make
    [f.close() for f in made]


def test_secret_reveals_but_never_prints():
    s = Secret("hunter2")
    assert s.reveal() == "hunter2"
    for shown in (repr(s), str(s), f"{s}", "%s" % s):  # noqa: UP031 - percent formatting is under test
        assert shown == "Secret(***)" and "hunter2" not in shown
    assert Secret("x") != "x"


def test_resolve_keyless_and_default_chain_return_none():
    creds = Credentials({})
    assert creds.resolve("none:local") is None
    assert creds.resolve("aws:default-chain") is None


def test_resolve_env_returns_secret():
    secret = Credentials({"FOO": "value-1"}).resolve("env:FOO")
    assert isinstance(secret, Secret) and secret.reveal() == "value-1"


@pytest.mark.parametrize("environ", [{}, {"FOO": ""}])
def test_missing_or_empty_env_names_variable_only(environ):
    with pytest.raises(CredentialError) as e:
        Credentials(environ).resolve("env:FOO")
    assert "FOO" in str(e.value)
    assert "value" not in str(e.value)


def test_unknown_scheme_rejected():
    with pytest.raises(CredentialError):
        Credentials({}).resolve("vault:x")


def test_openai_adapter_sends_resolved_bearer(fake):
    srv = fake([(200, OPENAI_OK)])
    p = make_profile("openai-compatible", srv.url + "/v1", cred="env:OPENAI_KEY")
    OpenAICompatibleAdapter(p, Credentials({"OPENAI_KEY": "sk-1"})).converse(system="s", messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)
    assert srv.requests[0][1]["authorization"] == "Bearer sk-1"


def test_anthropic_without_key_fails_before_http(fake):
    srv = fake([])
    p = make_profile("anthropic", srv.url, cred="none:local")
    with pytest.raises(CredentialError):
        AnthropicAdapter(p, Credentials({})).converse(system="s", messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)
    assert srv.requests == []
