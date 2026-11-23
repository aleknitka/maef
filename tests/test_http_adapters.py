import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml
from maef_api.adapters.anthropic import AnthropicAdapter
from maef_api.adapters.base import Message
from maef_api.adapters.http import ModelHTTPError
from maef_api.adapters.openai_compatible import OpenAICompatibleAdapter
from maef_api.credentials import CredentialError, Credentials
from maef_api.models import ModelEndpointProfile
from pydantic import ValidationError


def make_profile(kind, base_url, cred='none:local', extra_endpoint='', dialect=None):
    return ModelEndpointProfile.model_validate(yaml.safe_load(f"""
schema_version: maef.dev/v1alpha1
id: p-one
version: 0.1.0
display_name: T
vendor: T
api_dialect: {dialect or kind}
endpoint: {{kind: {kind}, base_url: "{base_url}", model_id: m-1{extra_endpoint}}}
credential_ref: {cred}
max_retries: 1
"""))

class Fake:
    """Local HTTP server that records requests and replays canned replies."""
    def __init__(self, replies):
        self.requests, outer = [], self
        replies = list(replies)
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                status, payload = replies.pop(0)
                self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *a): pass
        self.server = HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def close(self): self.server.shutdown()

@pytest.fixture
def fake():
    made = []
    def make(replies):
        f = Fake(replies); made.append(f); return f
    yield make
    [f.close() for f in made]

OPENAI_OK = {'choices': [{'message': {'content': 'Hi'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 2}}

def test_openai_request_and_parse(fake):
    srv = fake([(200, OPENAI_OK)])
    p = make_profile('openai-compatible', srv.url + '/v1', cred='env:OPENAI_API_KEY', extra_endpoint=', token_param: max_completion_tokens')
    out = OpenAICompatibleAdapter(p, Credentials({'OPENAI_API_KEY': 'sk-test'})).converse(system='sys', messages=[Message('user', 'q')], tools=[], max_output_tokens=50, timeout_seconds=5)
    assert (out.text, out.stop_reason, out.input_tokens, out.output_tokens) == ('Hi', 'end_turn', 7, 2)
    path, headers, body = srv.requests[0]
    assert path == '/v1/chat/completions' and headers['authorization'] == 'Bearer sk-test'
    assert body['max_completion_tokens'] == 50 and 'max_tokens' not in body
    assert body['messages'] == [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'q'}]

def test_openai_keyless_local_sends_no_auth(fake):
    srv = fake([(200, OPENAI_OK)])
    OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url + '/v1'), Credentials({})).converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)
    assert 'authorization' not in srv.requests[0][1] and srv.requests[0][2]['max_tokens'] == 5

def test_openai_missing_usage_fails(fake):
    srv = fake([(200, {'choices': [{'message': {'content': 'x'}, 'finish_reason': 'stop'}]})])
    with pytest.raises(ModelHTTPError, match='missing_usage'):
        OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})).converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)

def test_retries_5xx_then_succeeds_and_errors_hide_body(fake):
    srv = fake([(503, {'error': 'secret echo'}), (200, OPENAI_OK)])
    adapter = OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({}), sleep=lambda s: None)
    assert adapter.converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5).text == 'Hi'
    srv2 = fake([(401, {'error': 'secret echo'})])
    with pytest.raises(ModelHTTPError) as e:
        OpenAICompatibleAdapter(make_profile('openai-compatible', srv2.url), Credentials({})).converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)
    assert e.value.status == 401 and 'secret' not in str(e.value) and len(srv2.requests) == 1  # 4xx not retried

def test_missing_env_key_names_variable_only():
    p = make_profile('openai-compatible', 'https://api.openai.com/v1', cred='env:OPENAI_API_KEY')
    with pytest.raises(CredentialError, match='missing_credential:OPENAI_API_KEY'):
        OpenAICompatibleAdapter(p, Credentials({})).converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)

def test_anthropic_request_and_parse(fake):
    srv = fake([(200, {'content': [{'type': 'text', 'text': 'A'}, {'type': 'text', 'text': 'B'}], 'stop_reason': 'max_tokens',
                       'usage': {'input_tokens': 11, 'output_tokens': 5}})])
    p = make_profile('anthropic', srv.url, cred='env:ANTHROPIC_API_KEY')
    out = AnthropicAdapter(p, Credentials({'ANTHROPIC_API_KEY': 'ak-test'})).converse(system='sys', messages=[Message('user', 'q')], tools=[], max_output_tokens=9, timeout_seconds=5)
    assert (out.text, out.stop_reason, out.input_tokens, out.output_tokens) == ('AB', 'max_tokens', 11, 5)
    path, headers, body = srv.requests[0]
    assert path == '/v1/messages' and headers['x-api-key'] == 'ak-test' and headers['anthropic-version'] == '2023-06-01'
    assert body == {'model': 'm-1', 'max_tokens': 9, 'system': 'sys', 'messages': [{'role': 'user', 'content': 'q'}]}

@pytest.mark.parametrize('url', ['http://example.com/v1', 'https://user:pw@example.com', 'ftp://x', 'https://x.com/v1?key=1'])
def test_unsafe_base_urls_rejected(url):
    with pytest.raises(ValidationError):
        make_profile('openai-compatible', url)

def test_dialect_must_match_endpoint_and_secrets_rejected():
    with pytest.raises(ValidationError, match='does not match'):
        make_profile('anthropic', 'https://api.anthropic.com', cred='env:K_KEY', dialect='openai-compatible')
    with pytest.raises(ValidationError):
        make_profile('anthropic', 'https://api.anthropic.com', cred='sk-live-abc123')

def _call(adapter):
    return adapter.converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=5, timeout_seconds=5)

def test_openai_cache_tokens_tolerant_and_clamped(fake):
    usage = {'prompt_tokens': 2270, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 2176, 'cache_write_tokens': 0}}
    odd = [{'prompt_tokens': 10, 'completion_tokens': 1, 'prompt_tokens_details': d}
           for d in (None, {'cached_tokens': 'x', 'cache_write_tokens': True}, {'cached_tokens': 99, 'cache_write_tokens': 5})]
    srv = fake([(200, {**OPENAI_OK, 'usage': u}) for u in [usage, *odd]])
    adapter = OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({}))
    got = [(r.input_tokens, r.cache_read_tokens, r.cache_write_tokens) for r in (_call(adapter) for _ in range(4))]
    assert got == [(2270, 2176, 0), (10, 0, 0), (10, 0, 0), (10, 10, 0)]

def test_anthropic_input_includes_cache_tokens(fake):
    srv = fake([(200, {'content': [{'type': 'text', 'text': 'A'}], 'stop_reason': 'end_turn',
                       'usage': {'input_tokens': 3, 'output_tokens': 2, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': None}})])
    out = _call(AnthropicAdapter(make_profile('anthropic', srv.url, cred='env:KEY_X'), Credentials({'KEY_X': 'k'})))
    assert (out.input_tokens, out.cache_read_tokens, out.cache_write_tokens) == (103, 100, 0)
