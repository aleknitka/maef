import json
import time

import pytest
from maef_api import client as client_module
from maef_api.client import HttpTransport, MaefClient, MaefClientError
from maef_api.egress import EgressPolicy


def sse_frame(seq, type_):
    data = json.dumps({'seq': seq, 'type': type_, 'ts': 'now', 'payload': {}})
    return [f'id: {seq}'.encode(), f'event: {type_}'.encode(), f'data: {data}'.encode(), b'']


class ScriptedStreams:
    """Each stream() call replays the next script; an exception item drops that connection."""

    def __init__(self, *scripts):
        self.scripts, self.headers = list(scripts), []

    def request(self, method, path, *, json=None, headers=None):
        raise AssertionError('plain requests are not expected here')

    def stream(self, path, headers=None):
        self.headers.append(headers)
        for item in self.scripts.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


class FixedReply:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def request(self, method, path, *, json=None, headers=None):
        return self.status, {}, self.body


def test_calls_go_through_the_app(live_api):
    client = MaefClient(live_api.transport)
    assert 'research-analyst' in {a['id'] for a in client.list_agents()}
    preview = client.preview('research-analyst', 'Summarise',
                             {'max_tokens': 50_000, 'max_duration_seconds': 900, 'max_turns': 2})
    assert preview['effective_budget']['max_turns'] == 2
    run_id = client.submit('research-analyst', 'Hello')['run_id']
    assert client.get_run(run_id)['id'] == run_id
    assert [r['id'] for r in client.list_runs()] == [run_id]
    assert client.artifacts(run_id) == []
    live_api.finish_queued()
    refs = client.artifacts(run_id)
    assert [r['name'] for r in refs] == ['output.md']
    assert client.download(refs[0]['digest']) == b'Final answer.'
    other = client.submit('research-analyst', 'Second')['run_id']
    assert client.cancel(other)['outcome'] == 'cancelled'


def test_wait_returns_terminal_run_when_worker_finishes_in_background(live_api):
    client = MaefClient(live_api.transport)
    run_id = client.submit('research-analyst', 'Hello')['run_id']
    live_api.finish_later(0.1)
    seen = []
    assert client.wait(run_id, 10, on_event=seen.append)['status'] == 'succeeded'
    assert seen[-1]['type'] == 'task.succeeded'


def test_wait_without_worker_returns_live_run_at_timeout(live_api):
    client = MaefClient(live_api.transport)
    run_id = client.submit('research-analyst', 'Hello')['run_id']
    started = time.monotonic()
    assert client.wait(run_id, 0.3)['status'] == 'admitted'
    assert time.monotonic() - started < 5


def test_result_text_is_final_text_or_none(live_api):
    client = MaefClient(live_api.transport)
    run_id = client.submit('research-analyst', 'Hello')['run_id']
    assert client.result_text(run_id) is None  # queued: no output.md yet
    live_api.finish_queued()
    assert client.result_text(run_id) == 'Final answer.'
    live_api.adapter.error = RuntimeError('boom')
    failed = client.submit('research-analyst', 'Hello')['run_id']
    live_api.finish_queued()
    assert client.get_run(failed)['status'] == 'failed'
    assert client.result_text(failed) is None


def test_events_dedupe_by_seq_and_resume_with_last_event_id():
    first = sse_frame(1, 'task.created') + sse_frame(2, 'task.admitted') + [
        MaefClientError(0, 'connection_error', 'dropped')]
    second = sse_frame(2, 'task.admitted') + sse_frame(3, 'run.started') + sse_frame(4, 'task.succeeded')
    transport = ScriptedStreams(first, second)
    seqs = [e['seq'] for e in MaefClient(transport).events('run-1')]
    assert seqs == [1, 2, 3, 4]
    assert transport.headers == [None, {'Last-Event-ID': '2'}]


def test_error_carries_status_and_code_from_api_detail(live_api):
    client = MaefClient(live_api.transport)
    with pytest.raises(MaefClientError) as missing:
        client.get_run('no-such-run')
    assert (missing.value.status, missing.value.code) == (404, 'http_error')
    run_id = client.submit('research-analyst', 'Hello')['run_id']
    live_api.finish_queued()
    with pytest.raises(MaefClientError) as done:
        client.cancel(run_id)
    assert (done.value.status, done.value.code) == (409, 'already_terminal')


def test_error_message_is_capped_and_ignores_other_body_fields():
    body = json.dumps({'detail': 'x' * 1000}).encode()
    with pytest.raises(MaefClientError) as capped:
        MaefClient(FixedReply(400, body)).list_agents()
    assert capped.value.status == 400 and len(capped.value.message) == 300
    with pytest.raises(MaefClientError) as dropped:
        MaefClient(FixedReply(500, json.dumps({'prompt': 'SECRET'}).encode())).list_agents()
    assert dropped.value.message == 'Request failed.' and 'SECRET' not in str(dropped.value)


def test_http_transport_refuses_metadata_address_without_sending(monkeypatch):
    sent = []

    def opener_spy(req, timeout):
        sent.append(req)
        raise AssertionError('request must not be sent')

    def resolver(host, port, type=None):
        return [(2, 1, 6, '', ('169.254.169.254', port))]

    monkeypatch.setattr(client_module, 'opener', type('Opener', (), {'open': staticmethod(opener_spy)}))
    transport = HttpTransport('http://maef.example.test:8000', policy=EgressPolicy(resolver=resolver))
    with pytest.raises(MaefClientError) as exc:
        MaefClient(transport).list_agents()
    assert exc.value.code == 'egress_denied' and sent == []
