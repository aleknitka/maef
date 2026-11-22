import itertools
import json
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.stub import Stubber
from maef_api.adapters.anthropic import AnthropicAdapter
from maef_api.adapters.base import Message, ModelResponse, ToolCall, ToolSpec
from maef_api.adapters.bedrock import BedrockAdapter
from maef_api.adapters.openai_compatible import OpenAICompatibleAdapter
from maef_api.credentials import Credentials
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.ledger import Ledger
from maef_api.models import Budget, ModelEndpointProfile
from maef_api.tools import BuiltinTools
from test_http_adapters import Fake, make_profile
from test_run_execution import definition, profile

SPEC = ToolSpec('read_agent_file', 'Read a file.', {'type': 'object', 'properties': {'path': {'type': 'string'}},
                                                    'required': ['path']})
USAGE = {'prompt_tokens': 7, 'completion_tokens': 2}
FILES = {'notes/ARG-MARKER.md': b'RESULT-MARKER-123'}
EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'model-profiles'


@pytest.fixture
def fake_server():
    made = []
    def make(replies):
        f = Fake(replies); made.append(f); return f
    yield make
    for f in made:
        f.close()


def openai_reply(message, finish='stop'):
    return {'choices': [{'message': message, 'finish_reason': finish}], 'usage': USAGE}


def openai_call(srv, messages, tools=(SPEC,)):
    adapter = OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url + '/v1'), Credentials({}))
    return adapter.converse(system='s', messages=messages, tools=list(tools), max_output_tokens=5, timeout_seconds=5)


def tool_call_message(arguments, call_id='c1'):
    return {'id': call_id, 'type': 'function', 'function': {'name': 'read_agent_file', 'arguments': arguments}}


# --- OpenAI-compatible wire format and local-model quirks ---

def test_openai_tools_sent_in_function_form(fake_server):
    srv = fake_server([(200, openai_reply({'content': 'ok'}))])
    openai_call(srv, [Message('user', 'q')])
    assert srv.requests[0][2]['tools'] == [{'type': 'function', 'function': {
        'name': 'read_agent_file', 'description': 'Read a file.', 'parameters': SPEC.input_schema}}]


def test_openai_no_tools_key_when_no_tools(fake_server):
    srv = fake_server([(200, openai_reply({'content': 'ok'}))])
    openai_call(srv, [Message('user', 'q')], tools=())
    assert 'tools' not in srv.requests[0][2]


def test_openai_arguments_as_json_string(fake_server):
    srv = fake_server([(200, openai_reply({'content': None, 'tool_calls': [tool_call_message('{"path": "SYSTEM.md"}')]},
                                          'tool_calls'))])
    out = openai_call(srv, [Message('user', 'q')])
    assert out.tool_calls == (ToolCall('c1', 'read_agent_file', {'path': 'SYSTEM.md'}),)


def test_openai_arguments_as_parsed_object_ollama_style(fake_server):
    call = tool_call_message({'path': 'SYSTEM.md'})
    srv = fake_server([(200, openai_reply({'content': '', 'tool_calls': [call]}, 'tool_calls'))])
    out = openai_call(srv, [Message('user', 'q')])
    assert out.tool_calls == (ToolCall('c1', 'read_agent_file', {'path': 'SYSTEM.md'}),)


def test_openai_missing_call_id_is_generated(fake_server):
    call = tool_call_message('{"path": "a"}')
    del call['id']
    srv = fake_server([(200, openai_reply({'content': None, 'tool_calls': [call]}, 'tool_calls'))])
    assert openai_call(srv, [Message('user', 'q')]).tool_calls[0].id == 'call_0'


def test_openai_finish_stop_with_tool_calls_is_tool_use(fake_server):
    srv = fake_server([(200, openai_reply({'content': None, 'tool_calls': [tool_call_message('{}')]}, 'stop'))])
    assert openai_call(srv, [Message('user', 'q')]).stop_reason == 'tool_use'


def test_openai_null_content_is_empty_text(fake_server):
    srv = fake_server([(200, openai_reply({'content': None}))])
    out = openai_call(srv, [Message('user', 'q')])
    assert (out.text, out.stop_reason) == ('', 'end_turn')


def test_openai_invalid_json_arguments_set_parse_error(fake_server):
    srv = fake_server([(200, openai_reply({'content': None, 'tool_calls': [tool_call_message('{"path": ')]}, 'tool_calls'))])
    out = openai_call(srv, [Message('user', 'q')])
    assert out.tool_calls == (ToolCall('c1', 'read_agent_file', {}, parse_error='invalid_arguments'),)


def test_openai_round_trip_serialises_assistant_and_tool_messages(fake_server):
    srv = fake_server([
        (200, openai_reply({'content': None, 'tool_calls': [tool_call_message('{"path": "SYSTEM.md"}', 'c9')]}, 'tool_calls')),
        (200, openai_reply({'content': 'Done'})),
    ])
    first = openai_call(srv, [Message('user', 'q')])
    openai_call(srv, [Message('user', 'q'), Message('assistant', first.text, first.tool_calls),
                      Message('tool', 'pinned', tool_call_id='c9')])
    assert srv.requests[1][2]['messages'][2:] == [  # [0] system, [1] user
        {'role': 'assistant', 'content': None, 'tool_calls': [tool_call_message('{"path": "SYSTEM.md"}', 'c9')]},
        {'role': 'tool', 'tool_call_id': 'c9', 'content': 'pinned'},
    ]


# --- Anthropic wire format ---

def test_anthropic_tools_use_input_schema(fake_server):
    srv = fake_server([(200, {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'read_agent_file', 'input': {'path': 'a'}}],
                              'stop_reason': 'tool_use', 'usage': {'input_tokens': 3, 'output_tokens': 1}})])
    adapter = AnthropicAdapter(make_profile('anthropic', srv.url, cred='env:ANTHROPIC_API_KEY'), Credentials({'ANTHROPIC_API_KEY': 'ak'}))
    out = adapter.converse(system='s', messages=[Message('user', 'q')], tools=[SPEC], max_output_tokens=5, timeout_seconds=5)
    assert out.stop_reason == 'tool_use' and out.tool_calls == (ToolCall('t1', 'read_agent_file', {'path': 'a'}),)
    assert srv.requests[0][2]['tools'] == [{'name': 'read_agent_file', 'description': 'Read a file.',
                                            'input_schema': SPEC.input_schema}]


def test_anthropic_consecutive_tool_results_merge_into_one_user_message(fake_server):
    srv = fake_server([(200, {'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn',
                              'usage': {'input_tokens': 1, 'output_tokens': 1}})])
    adapter = AnthropicAdapter(make_profile('anthropic', srv.url, cred='env:ANTHROPIC_API_KEY'), Credentials({'ANTHROPIC_API_KEY': 'ak'}))
    calls = (ToolCall('t1', 'read_agent_file', {'path': 'a.md'}), ToolCall('t2', 'read_agent_file', {'path': 'b.md'}))
    adapter.converse(system='s', messages=[
        Message('user', 'q'), Message('assistant', '', calls),
        Message('tool', 'A', tool_call_id='t1'), Message('tool', 'file_not_available', tool_call_id='t2', is_error=True),
    ], tools=[SPEC], max_output_tokens=5, timeout_seconds=5)
    assert srv.requests[0][2]['messages'] == [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'read_agent_file', 'input': {'path': 'a.md'}},
            {'type': 'tool_use', 'id': 't2', 'name': 'read_agent_file', 'input': {'path': 'b.md'}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'A', 'is_error': False},
            {'type': 'tool_result', 'tool_use_id': 't2', 'content': 'file_not_available', 'is_error': True}]},
    ]


# --- Bedrock (Stubber) ---

def bedrock_client():
    return boto3.client('bedrock-runtime', region_name='eu-north-1', aws_access_key_id='x', aws_secret_access_key='y')


TOOL_CONFIG = {'tools': [{'toolSpec': {'name': 'read_agent_file', 'description': 'Read a file.',
                                       'inputSchema': {'json': SPEC.input_schema}}}]}


def test_bedrock_tool_config_present_only_with_tools():
    client = bedrock_client()
    adapter = BedrockAdapter(profile(), client=client)
    base = {'modelId': 'global.test.model-1', 'system': [{'text': 's'}],
            'messages': [{'role': 'user', 'content': [{'text': 'q'}]}], 'inferenceConfig': {'maxTokens': 64}}
    usage = {'inputTokens': 4, 'outputTokens': 2, 'totalTokens': 6}
    with Stubber(client) as stub:
        stub.add_response('converse', {
            'output': {'message': {'role': 'assistant', 'content': [
                {'toolUse': {'toolUseId': 't1', 'name': 'read_agent_file', 'input': {'path': 'SYSTEM.md'}}}]}},
            'stopReason': 'tool_use', 'usage': usage, 'metrics': {'latencyMs': 1}},
            expected_params={**base, 'toolConfig': TOOL_CONFIG})
        stub.add_response('converse', {
            'output': {'message': {'role': 'assistant', 'content': [{'text': 'ok'}]}},
            'stopReason': 'end_turn', 'usage': usage, 'metrics': {'latencyMs': 1}},
            expected_params=base)
        first = adapter.converse(system='s', messages=[Message('user', 'q')], tools=[SPEC], max_output_tokens=64, timeout_seconds=30)
        adapter.converse(system='s', messages=[Message('user', 'q')], tools=[], max_output_tokens=64, timeout_seconds=30)
    assert first.stop_reason == 'tool_use'
    assert first.tool_calls == (ToolCall('t1', 'read_agent_file', {'path': 'SYSTEM.md'}),)


def test_bedrock_error_result_sent_with_error_status():
    client = bedrock_client()
    adapter = BedrockAdapter(profile(), client=client)
    messages = [Message('user', 'q'), Message('assistant', '', (ToolCall('t1', 'read_agent_file', {'path': 'x'}),)),
                Message('tool', 'file_not_available', tool_call_id='t1', is_error=True)]
    with Stubber(client) as stub:
        stub.add_response('converse', {
            'output': {'message': {'role': 'assistant', 'content': [{'text': 'Noted.'}]}},
            'stopReason': 'end_turn', 'usage': {'inputTokens': 1, 'outputTokens': 1, 'totalTokens': 2},
            'metrics': {'latencyMs': 1}},
            expected_params={
                'modelId': 'global.test.model-1', 'system': [{'text': 's'}], 'inferenceConfig': {'maxTokens': 64},
                'toolConfig': TOOL_CONFIG,
                'messages': [
                    {'role': 'user', 'content': [{'text': 'q'}]},
                    {'role': 'assistant', 'content': [{'toolUse': {'toolUseId': 't1', 'name': 'read_agent_file',
                                                                   'input': {'path': 'x'}}}]},
                    {'role': 'user', 'content': [{'toolResult': {'toolUseId': 't1',
                                                                 'content': [{'text': 'file_not_available'}],
                                                                 'status': 'error'}}]},
                ]})
        adapter.converse(system='s', messages=messages, tools=[SPEC], max_output_tokens=64, timeout_seconds=30)


# --- Harness loop with a scripted adapter and a real Ledger ---

class Scripted:
    def __init__(self, responses):
        self._responses, self.calls = iter(responses), []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._responses)


class SpyTools:
    def __init__(self, inner):
        self.inner, self.invoked = inner, []

    def specs(self):
        return self.inner.specs()

    def invoke(self, name, arguments):
        self.invoked.append(name)
        return self.inner.invoke(name, arguments)


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / 'loop.db')
    s.ensure_account('default', 5_000_000)
    return s


def run_loop(store, responses, *, tools=None, max_turns=5):
    d = definition()
    budget = Budget(max_tokens=200_000, max_turns=max_turns)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction='Question?')
    tools = tools or BuiltinTools(['read_agent_file', 'current_time'], FILES)
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, 'Question?', tools)
    adapter = Scripted(responses)
    execute(LedgerSink(store, run_id), run, adapter)
    return store.get(run_id), adapter


def test_tool_use_then_end_turn_succeeds_and_keeps_content_out_of_events(store):
    detail, adapter = run_loop(store, [
        ModelResponse('', (ToolCall('c1', 'read_agent_file', {'path': 'notes/ARG-MARKER.md'}),), 'tool_use', 10, 5),
        ModelResponse('All done.', (), 'end_turn', 20, 6),
    ])
    assert detail.status == 'succeeded'
    types = [e.type for e in detail.events]
    assert 'tool.call' in types and 'tool.result' in types
    tool_msg = adapter.calls[1]['messages'][-1]
    assert (tool_msg.role, tool_msg.text, tool_msg.tool_call_id) == ('tool', 'RESULT-MARKER-123', 'c1')
    dump = json.dumps([e.payload for e in detail.events])
    assert 'ARG-MARKER' not in dump and 'RESULT-MARKER' not in dump


def test_endless_tool_calls_stop_at_turns_exhausted(store):
    call = ModelResponse('', (ToolCall('c1', 'current_time', {}),), 'tool_use', 10, 5)
    detail, adapter = run_loop(store, itertools.repeat(call), max_turns=3)
    assert detail.status == 'failed' and detail.terminal_reason == 'turns_exhausted'
    assert len(adapter.calls) == 3


def test_parse_error_call_not_invoked_and_error_sent_back(store):
    tools = SpyTools(BuiltinTools(['read_agent_file'], FILES))
    detail, adapter = run_loop(store, [
        ModelResponse('', (ToolCall('c1', 'read_agent_file', {}, parse_error='invalid_arguments'),), 'tool_use', 10, 5),
        ModelResponse('Retrying.', (), 'end_turn', 10, 5),
    ], tools=tools)
    assert tools.invoked == []
    msg = adapter.calls[1]['messages'][-1]
    assert (msg.role, msg.text, msg.is_error, msg.tool_call_id) == ('tool', 'invalid_arguments', True, 'c1')
    assert detail.status == 'succeeded'


def test_unknown_tool_name_gives_error_result_and_run_continues(store):
    detail, adapter = run_loop(store, [
        ModelResponse('', (ToolCall('c1', 'shell', {'cmd': 'ls'}),), 'tool_use', 10, 5),
        ModelResponse('Ok.', (), 'end_turn', 10, 5),
    ])
    msg = adapter.calls[1]['messages'][-1]
    assert msg.is_error is True and msg.text == 'unknown_tool'
    assert detail.status == 'succeeded'
    assert [e.payload['is_error'] for e in detail.events if e.type == 'tool.result'] == [True]


def test_tokens_used_is_sum_of_all_turns(store):
    detail, _ = run_loop(store, [
        ModelResponse('', (ToolCall('c1', 'current_time', {}),), 'tool_use', 100, 10),
        ModelResponse('', (ToolCall('c2', 'read_agent_file', {'path': 'notes/ARG-MARKER.md'}),), 'tool_use', 150, 20),
        ModelResponse('Done.', (), 'end_turn', 200, 30),
    ])
    assert detail.status == 'succeeded'
    assert detail.tokens_used == (100 + 10) + (150 + 20) + (200 + 30) == 510


# --- Example model profiles ---

@pytest.mark.parametrize('name', ['ollama.yaml', 'lmstudio.yaml'])
def test_example_profiles_validate_loopback_and_keyless(name):
    p = ModelEndpointProfile.model_validate(yaml.safe_load((EXAMPLES / name).read_text()))
    assert p.api_dialect == 'openai-compatible'
    assert p.endpoint.is_loopback is True
    assert p.credential_ref == 'none:local'


def test_openai_length_with_tool_calls_is_truncation(fake_server):
    reply = {'choices': [{'message': {'content': None, 'tool_calls': [
        {'id': 'c1', 'type': 'function', 'function': {'name': 'current_time', 'arguments': '{"pa'}}]},
        'finish_reason': 'length'}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 9}}
    srv = fake_server([(200, reply)])
    out = OpenAICompatibleAdapter(make_profile('openai-compatible', srv.url), Credentials({})).converse(
        system='s', messages=[Message(role='user', text='q')], tools=[], max_output_tokens=9, timeout_seconds=5)
    assert out.stop_reason == 'max_tokens'
