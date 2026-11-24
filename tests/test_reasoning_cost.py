"""Reasoning tokens, provider-reported cost, reply identity and reasoning_effort.

Covers the OpenAI-compatible adapter's parsing (against the local fake server from test_http_adapters),
the reasoning_effort profile field, the harness's timing and usage events, the Prometheus counters and
the OTel attributes. Stubs and a local HTTP server only; nothing leaves the machine.
"""
import json

import pytest
from fastapi.testclient import TestClient
from maef_api import main, otel
from maef_api.adapters.base import (
    Message,
    ModelResponse,
    ToolCall,
    ToolSpec,
    cost_value,
    short_str,
)
from maef_api.adapters.http import ModelHTTPError
from maef_api.adapters.openai_compatible import OpenAICompatibleAdapter
from maef_api.credentials import Credentials
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.ledger import Ledger
from maef_api.models import Budget
from maef_api.tools import ToolResult
from pydantic import ValidationError
from test_http_adapters import Fake, make_profile
from test_run_execution import definition

INSTRUCTION = 'INSTRUCTION-MARK-1'
OPENROUTER_REPLY = {
    'id': 'gen-1729', 'model': 'openai/gpt-4o-mini', 'provider': 'OpenAI',
    'choices': [{'message': {'role': 'assistant', 'content': 'Hi there', 'reasoning': 'Think first.'},
                 'finish_reason': 'stop'}],
    'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70, 'cost': 0.00042,
              'prompt_tokens_details': {'cached_tokens': 10},
              'completion_tokens_details': {'reasoning_tokens': 12}},
}


@pytest.fixture
def fake_server():
    made = []

    def make(replies):
        srv = Fake(replies)
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.close()


def call(adapter):
    return adapter.converse(system='sys', messages=[Message('user', 'q')], tools=[], max_output_tokens=20,
                            timeout_seconds=5)


def with_usage(**usage):
    return {**OPENROUTER_REPLY, 'usage': {**OPENROUTER_REPLY['usage'], **usage}}


# --- 1. parsing a full OpenRouter-style reply ---

def test_openrouter_style_reply_is_fully_parsed(fake_server):
    srv = fake_server([(200, OPENROUTER_REPLY)])
    out = call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url + '/v1'), Credentials({})))
    assert out == ModelResponse(
        text='Hi there', tool_calls=(), stop_reason='end_turn', input_tokens=50, output_tokens=20,
        cache_read_tokens=10, cache_write_tokens=0, reasoning_tokens=12, cost_usd=0.00042,
        provider='OpenAI', response_id='gen-1729', model='openai/gpt-4o-mini', reasoning='Think first.')


def test_zero_cost_is_kept_as_zero_not_none(fake_server):
    srv = fake_server([(200, with_usage(cost=0))])
    assert call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({}))).cost_usd == 0.0


def test_missing_cost_and_reasoning_details_default_to_none_and_zero(fake_server):
    usage = {'prompt_tokens': 5, 'completion_tokens': 2}  # a plain server: no cost, no details
    srv = fake_server([(200, {'choices': [{'message': {'content': 'x'}, 'finish_reason': 'stop'}], 'usage': usage})])
    out = call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})))
    assert (out.cost_usd, out.reasoning_tokens, out.provider, out.response_id, out.model, out.reasoning) == (
        None, 0, None, None, None, '')


# --- 2. missing and garbage fields fall back to safe defaults ---

@pytest.mark.parametrize('value', [True, False, 'free', '0.5', -0.5, float('nan'), float('inf'), float('-inf'),
                                   [0.1], {'usd': 0.1}, None])
def test_garbage_cost_becomes_none(fake_server, value):
    srv = fake_server([(200, with_usage(cost=value))])
    out = call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})))
    assert out.cost_usd is None
    assert (out.input_tokens, out.output_tokens, out.text) == (50, 20, 'Hi there')  # the rest still parses


@pytest.mark.parametrize('value', ['x', True, -4, None, 1.5, [3], {'n': 3}])
def test_garbage_reasoning_tokens_become_zero(fake_server, value):
    details = {'completion_tokens_details': {'reasoning_tokens': value}}
    srv = fake_server([(200, with_usage(**details))])
    assert call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({}))).reasoning_tokens == 0


def test_reasoning_tokens_above_completion_tokens_are_capped(fake_server):
    srv = fake_server([(200, with_usage(completion_tokens_details={'reasoning_tokens': 999}))])
    out = call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})))
    assert out.reasoning_tokens == out.output_tokens == 20


def test_non_string_identity_fields_and_reasoning_become_none_or_empty(fake_server):
    reply = {**OPENROUTER_REPLY, 'provider': {'name': 'x'}, 'id': ['gen'], 'model': 42,
             'choices': [{'message': {'content': None, 'reasoning': ['not', 'text']}, 'finish_reason': 'stop'}]}
    srv = fake_server([(200, reply)])
    out = call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})))
    assert (out.provider, out.response_id, out.model, out.reasoning, out.text) == (None, None, None, '', '')


def test_long_provider_string_is_truncated_to_200(fake_server):
    srv = fake_server([(200, {**OPENROUTER_REPLY, 'provider': 'P' * 1000})])
    out = call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})))
    assert out.provider == 'P' * 200


@pytest.mark.parametrize('value', [None, 42, ['a'], {'a': 1}])
def test_short_str_returns_none_for_non_strings(value):
    assert short_str(value) is None


def test_short_str_keeps_short_strings_and_cuts_long_ones():
    assert short_str('short') == 'short'
    assert short_str('a' * 500) == 'a' * 200


@pytest.mark.parametrize('value', [True, False, 'free', None, -0.5, float('nan'), float('inf'), float('-inf'), [1], {}])
def test_cost_value_maps_non_finite_negative_and_non_numbers_to_none(value):
    assert cost_value(value) is None


@pytest.mark.parametrize('value, expected', [(0, 0.0), (7, 7.0), (0.25, 0.25), (1e-9, 1e-9)])
def test_cost_value_keeps_finite_non_negative_numbers(value, expected):
    assert cost_value(value) == expected


@pytest.mark.parametrize('field, value', [('prompt_tokens', '50'), ('completion_tokens', '20'),
                                          ('prompt_tokens', -5), ('completion_tokens', True)])
def test_garbage_token_counts_are_not_passed_through(fake_server, field, value):
    srv = fake_server([(200, with_usage(**{field: value}))])
    adapter = OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({}))
    try:
        out = call(adapter)
    except ModelHTTPError:
        return  # refusing the reply is acceptable: nothing unmeterable is recorded
    assert type(out.input_tokens) is int and out.input_tokens >= 0
    assert type(out.output_tokens) is int and out.output_tokens >= 0


# --- 3. reasoning_effort ---

@pytest.mark.parametrize('value', ['minimal', 'low', 'medium', 'high'])
def test_reasoning_effort_is_sent_when_set(fake_server, value):
    srv = fake_server([(200, OPENROUTER_REPLY)])
    profile = make_profile('openai-compatible', srv.url, extra_endpoint=f", reasoning_effort: '{value}'")
    call(OpenAICompatibleAdapter(profile, Credentials({})))
    assert srv.requests[0][2]['reasoning_effort'] == value


def test_reasoning_effort_is_absent_from_the_body_when_not_set(fake_server):
    srv = fake_server([(200, OPENROUTER_REPLY), (200, OPENROUTER_REPLY)])
    call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})))
    call(OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url, extra_endpoint=', reasoning_effort: null'),
                                 Credentials({})))
    assert all('reasoning_effort' not in body for _, _, body in srv.requests)


@pytest.mark.parametrize('value', ['extreme', 'HIGH', 'none', 'off', 'max', '0'])
def test_invalid_reasoning_effort_is_rejected_by_the_profile(value):
    with pytest.raises(ValidationError):
        make_profile('openai-compatible', 'https://api.openai.com/v1',
                     extra_endpoint=f", reasoning_effort: '{value}'")


# --- 4. harness events: timing, reasoning, cost, and no text ---

class Clock:
    """A clock the test moves by hand; execute() reads it through its clock argument."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Timed:
    """A model stub: each call takes `seconds` on the fake clock."""

    def __init__(self, clock, responses, seconds=0.25):
        self.clock, self.seconds, self.responses = clock, seconds, list(responses)

    def converse(self, **kwargs):
        self.clock.now += self.seconds
        return self.responses.pop(0)


class SlowLookup:
    """A tool that takes half a second on the fake clock."""

    def __init__(self, clock):
        self.clock = clock

    def specs(self):
        return [ToolSpec('lookup', 'Look a key up.', {'type': 'object'})]

    def invoke(self, name, arguments):
        self.clock.now += 0.5
        return ToolResult('RESULT-MARK', False)


TOOL_TURN = ModelResponse(
    text='', tool_calls=(ToolCall('c1', 'lookup', {'key': 'ARG-MARK'}),), stop_reason='tool_use',
    input_tokens=100, output_tokens=10, reasoning_tokens=4, cost_usd=0.001,
    provider='Parasail', response_id='gen-7', model='m-x', reasoning='REASONING-MARK')
FINAL = ModelResponse(text='FINAL-MARK', tool_calls=(), stop_reason='end_turn',
                      input_tokens=200, output_tokens=30, reasoning_tokens=9)


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / 'reasoning.db')
    s.ensure_account('default', 5_000_000)
    return s


def run_timed(store, responses, clock, *, tools=None):
    d = definition()
    budget = Budget(max_tokens=200_000, max_turns=5)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=INSTRUCTION)
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, INSTRUCTION, tools)
    execute(LedgerSink(store, run_id), run, Timed(clock, responses), clock=clock)
    return store.get(run_id)


def payloads(run, type_):
    return [e.payload for e in run.events if e.type == type_]


def test_model_and_tool_events_carry_exact_durations(store):
    clock = Clock()
    run = run_timed(store, [TOOL_TURN, FINAL], clock, tools=SlowLookup(clock))
    assert run.status == 'succeeded'
    assert payloads(run, 'model.response') == [
        {'turn': 1, 'stop_reason': 'tool_use', 'tool_calls': 1, 'latency_ms': 250,
         'provider': 'Parasail', 'response_id': 'gen-7', 'model': 'm-x'},
        {'turn': 2, 'stop_reason': 'end_turn', 'tool_calls': 0, 'latency_ms': 250},  # no ids reported: none sent
    ]
    assert payloads(run, 'tool.result') == [
        {'turn': 1, 'name': 'lookup', 'result_bytes': len('RESULT-MARK'), 'is_error': False, 'duration_ms': 500}]


def test_usage_events_carry_reasoning_always_and_cost_only_when_reported(store):
    run = run_timed(store, [TOOL_TURN, FINAL], Clock(), tools=None)
    usage = payloads(run, 'usage.recorded')
    assert usage[0] == {'turn': 1, 'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                        'cache_read_tokens': 0, 'cache_write_tokens': 0, 'reasoning_tokens': 4, 'cost_usd': 0.001}
    assert usage[1] == {'turn': 2, 'input_tokens': 200, 'output_tokens': 30, 'total_tokens': 230,
                        'cache_read_tokens': 0, 'cache_write_tokens': 0, 'reasoning_tokens': 9}
    assert 'cost_usd' not in usage[1]


def test_events_never_contain_prompt_reasoning_or_response_text(store):
    run = run_timed(store, [TOOL_TURN, FINAL], Clock(), tools=None)
    dump = json.dumps([e.model_dump() for e in run.events])
    for marker in (INSTRUCTION, 'ARG-MARK', 'REASONING-MARK', 'FINAL-MARK', 'You are an analyst.'):
        assert marker not in dump


# --- 5. Prometheus counters (follows test_cache_tokens.py) ---

@pytest.fixture
def metrics_store(tmp_path, monkeypatch):
    s = Ledger(tmp_path / 'reasoning-metrics.db')
    s.ensure_account('default', 5_000_000)
    monkeypatch.setattr(main, 'ledger', s)
    return s


def metric_value(text, sample):
    for line in text.splitlines():
        if line.startswith(sample + ' '):
            return float(line.split()[-1])
    raise AssertionError(f'{sample} not in metrics output')


def run_with(store, responses):
    d = definition()
    budget = Budget(max_tokens=200_000, max_turns=5)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=INSTRUCTION)
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, INSTRUCTION)
    execute(LedgerSink(store, run_id), run, Timed(Clock(), responses))


def test_metrics_sum_reasoning_tokens_and_cost_across_runs(metrics_store):
    run_with(metrics_store, [ModelResponse(text='a', tool_calls=(), stop_reason='end_turn', input_tokens=100,
                                           output_tokens=10, reasoning_tokens=4, cost_usd=0.0012)])
    run_with(metrics_store, [ModelResponse(text='b', tool_calls=(), stop_reason='end_turn', input_tokens=200,
                                           output_tokens=30, reasoning_tokens=9, cost_usd=0.0008)])
    run_with(metrics_store, [ModelResponse(text='c', tool_calls=(), stop_reason='end_turn', input_tokens=5,
                                           output_tokens=1)])  # reports neither: contributes nothing
    text = TestClient(main.app).get('/api/v1/metrics').text
    assert '# TYPE maef_reasoning_tokens_total counter' in text
    assert '# TYPE maef_cost_usd_total counter' in text
    assert metric_value(text, 'maef_reasoning_tokens_total') == 13
    assert metric_value(text, 'maef_cost_usd_total') == pytest.approx(0.002)


# --- 6. OTel attributes (follows test_observability.py: run_to_spans on a real ledger run) ---

def test_chat_spans_carry_reasoning_cost_provider_and_latency(store):
    clock = Clock()
    run = run_timed(store, [TOOL_TURN, FINAL], clock, tools=SlowLookup(clock))
    spans = otel.run_to_spans(run)
    root = next(s for s in spans if s.name == 'maef.run')
    chats = sorted((s for s in spans if s.name.startswith('chat ')), key=lambda s: s.attributes['maef.turn'])
    first, second = chats[0].attributes, chats[1].attributes
    assert (first['maef.usage.reasoning_tokens'], first['maef.usage.cost_usd']) == (4, 0.001)
    assert (first['gen_ai.response.id'], first['gen_ai.response.model'], first['maef.provider']) == ('gen-7', 'm-x', 'Parasail')
    assert first['maef.latency_ms'] == 250
    assert second['maef.usage.reasoning_tokens'] == 9
    assert 'maef.usage.cost_usd' not in second  # nothing reported for turn 2
    assert root.attributes['maef.usage.reasoning_tokens'] == 13
    assert root.attributes['maef.usage.cost_usd'] == pytest.approx(0.001)


def test_root_span_omits_cost_when_no_turn_reported_it(store):
    run = run_timed(store, [FINAL], Clock())
    root = next(s for s in otel.run_to_spans(run) if s.name == 'maef.run')
    assert 'maef.usage.cost_usd' not in root.attributes
    assert root.attributes['maef.usage.reasoning_tokens'] == 9


def test_cost_is_exported_as_an_otlp_double(store):
    run = run_timed(store, [TOOL_TURN, FINAL], Clock())
    body = otel.to_otlp(otel.run_to_spans(run))
    root = body['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
    cost = next(a for a in root['attributes'] if a['key'] == 'maef.usage.cost_usd')
    assert cost['value'] == {'doubleValue': pytest.approx(0.001)}
