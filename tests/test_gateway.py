import io
import json
import re

from maef_api.client import MaefClient
from maef_api.gateway import MAX_RESULT_BYTES, TOOLS, Gateway

AGENT = 'research-analyst'


def rpc(client, *messages):
    """Feed newline-delimited requests (dicts, or raw bytes for malformed lines); return parsed stdout lines."""
    payload = b''.join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b'\n' for m in messages)
    stdout = io.BytesIO()
    assert Gateway(client, io.BytesIO(payload), stdout).serve() == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def call(client, name, **arguments):
    [reply] = rpc(client, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}})
    return reply['result']


def text_of(result):
    return result['content'][0]['text']


def wrapped(run_id, text, status='succeeded'):
    return f'<maef-result run_id="{run_id}" status="{status}">\n{text}\n</maef-result>'


def test_initialize_echoes_supported_version_and_falls_back(live_api):
    client = MaefClient(live_api.transport)
    [known] = rpc(client, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}})
    assert known['result']['protocolVersion'] == '2025-03-26'
    [unknown] = rpc(client, {'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {'protocolVersion': '1999-01-01'}})
    assert unknown['result']['protocolVersion'] == '2025-06-18'


def test_tools_list_has_exactly_five_object_schemas(live_api):
    [reply] = rpc(MaefClient(live_api.transport), {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    tools = reply['result']['tools']
    assert [t['name'] for t in tools] == ['list_agents', 'delegate', 'run_status', 'run_result', 'cancel_run']
    assert len(TOOLS) == 5 and all(t['inputSchema']['type'] == 'object' for t in tools)


def test_ping_and_protocol_errors(live_api):
    client = MaefClient(live_api.transport)
    [ping] = rpc(client, {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
    assert ping['result'] == {}
    [unknown] = rpc(client, {'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list'})
    assert unknown['error']['code'] == -32601
    [malformed] = rpc(client, b'{not json')
    assert malformed['error']['code'] == -32700


def test_notifications_get_no_response(live_api):
    assert rpc(MaefClient(live_api.transport), {'jsonrpc': '2.0', 'method': 'notifications/initialized'}) == []


def test_delegate_without_wait_returns_queued_run_id(live_api):
    client = MaefClient(live_api.transport)
    text = text_of(call(client, 'delegate', agent_id=AGENT, instruction='Hello', wait_seconds=0))
    match = re.fullmatch(r'run_id=(\S+) status=admitted', text)
    assert match
    assert client.get_run(match.group(1))['status'] == 'admitted'


def test_delegate_with_wait_returns_status_and_wrapped_text(live_api):
    client = MaefClient(live_api.transport)
    live_api.finish_later(0.1)
    result = call(client, 'delegate', agent_id=AGENT, instruction='Hello', wait_seconds=5)
    text = text_of(result)
    run_id = re.search(r'run_id=(\S+)', text).group(1)
    assert 'status=succeeded' in text.splitlines()[0]
    assert wrapped(run_id, 'Final answer.') in text
    assert 'isError' not in result


def test_oversized_result_is_truncated_with_marker(live_api):
    client = MaefClient(live_api.transport)
    live_api.adapter.text = 'a' * (MAX_RESULT_BYTES + 5000)
    live_api.finish_later(0.1)
    text = text_of(call(client, 'delegate', agent_id=AGENT, instruction='Hello', wait_seconds=5))
    assert '[truncated: result exceeds 100 KiB]' in text
    assert len(text.encode()) < MAX_RESULT_BYTES + 1000


def test_run_result_wraps_finished_run_and_reports_unfinished(live_api):
    client = MaefClient(live_api.transport)
    run_id = client.submit(AGENT, 'Hello')['run_id']
    assert 'has not finished' in text_of(call(client, 'run_result', run_id=run_id))
    live_api.finish_queued()
    assert wrapped(run_id, 'Final answer.') in text_of(call(client, 'run_result', run_id=run_id))


def test_cancel_run_on_queued_run_reports_cancelled(live_api):
    client = MaefClient(live_api.transport)
    run_id = client.submit(AGENT, 'Hello')['run_id']
    assert text_of(call(client, 'cancel_run', run_id=run_id)) == f'run_id={run_id} outcome=cancelled'


def test_api_and_argument_errors_are_short_tool_errors(live_api):
    client = MaefClient(live_api.transport)
    unknown = call(client, 'delegate', agent_id='no-such-agent', instruction='Hello')
    assert unknown['isError'] is True
    assert text_of(unknown) == 'Agent not found (http_error)'
    missing = call(client, 'delegate', agent_id=AGENT)
    assert missing['isError'] is True and 'instruction is required' in text_of(missing)
    assert call(client, 'run_status', run_id='nope')['isError'] is True


def test_list_agents_exposes_no_instructions_or_mcp_details(live_api):
    text = text_of(call(MaefClient(live_api.transport), 'list_agents'))
    assert AGENT in text
    for leaked in ('Distinguish evidence', 'fetch@0.1.0', 'SYSTEM.md', 'env:', 'aws:', 'credential'):
        assert leaked not in text


def test_only_json_rpc_reaches_stdout(live_api, capsys):
    client = MaefClient(live_api.transport)
    live_api.finish_later(0.1)
    replies = rpc(
        client,
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'delegate', 'arguments': {'agent_id': AGENT, 'instruction': 'Hi', 'wait_seconds': 5}}},
        b'{bad json',
        {'jsonrpc': '2.0', 'id': 2, 'method': 'nope'},
    )
    assert [r.get('id') for r in replies] == [1, None, 2]
    assert capsys.readouterr().out == ''
