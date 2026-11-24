"""The opt-in transcript.jsonl artifact (harness + worker): off by default, record order and fields as
documented in packages/contracts/CONTRACTS.md, the end record on every path, truncation, best-effort
storage, and the MAEF_RECORD_TRANSCRIPT switch on the worker. Stubs and tmp ledgers only."""
import json

import pytest
from maef_api import harness
from maef_api.adapters.base import ModelResponse, ToolCall, ToolSpec
from maef_api.artifacts import ArtifactRejected
from maef_api.channel import LocalChannel
from maef_api.definitions import compute_definition_digest
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.ledger import Ledger
from maef_api.models import Budget
from maef_api.tools import ToolResult
from maef_api.worker import Worker
from test_run_execution import FakeAdapter, definition

INSTRUCTION = 'INSTRUCTION-MARK-2'
ASSISTANT_KEYS = {'type', 'turn', 't', 'latency_ms', 'stop_reason', 'text', 'reasoning', 'tool_calls', 'usage',
                  'provider', 'response_id', 'model'}
USAGE_KEYS = {'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens',
              'cost_usd'}
TOOL_KEYS = {'type', 'turn', 't', 'duration_ms', 'id', 'name', 'arguments', 'content', 'is_error'}
END_KEYS = {'type', 't', 'status', 'terminal_reason'}

TOOL_TURN = ModelResponse(
    text='', tool_calls=(ToolCall('c1', 'lookup', {'key': 'k1'}),), stop_reason='tool_use',
    input_tokens=100, output_tokens=10, reasoning_tokens=4, cost_usd=0.001,
    provider='Parasail', response_id='gen-7', model='m-x', reasoning='Thinking it over.')
FINAL = ModelResponse(text='FINAL-TEXT', tool_calls=(), stop_reason='end_turn', input_tokens=200, output_tokens=30)


class Lookup:
    def specs(self):
        return [ToolSpec('lookup', 'Look a key up.', {'type': 'object'})]

    def invoke(self, name, arguments):
        return ToolResult('RESULT-TEXT', False)


class Scripted:
    """Returns each step in turn; an exception step is raised instead."""

    def __init__(self, *steps):
        self.steps = list(steps)

    def converse(self, **kwargs):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / 'transcript.db')
    s.ensure_account('default', 5_000_000)
    return s


def run_with(store, adapter, *, record, tools=None, instruction=INSTRUCTION, sink_cls=LedgerSink):
    d = definition()
    budget = Budget(max_tokens=200_000, max_turns=5)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=instruction)
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, instruction, tools,
                      record_transcript=record)
    sink = sink_cls(store, run_id)
    execute(sink, run, adapter)
    return store.get(run_id), sink


def transcript_ref(store, run_id):
    return next((a for a in store.artifacts(run_id) if a.name == 'transcript.jsonl'), None)


def transcript(store, sink, run_id):
    """The parsed records, one per line; a line that is not JSON fails the test."""
    ref = transcript_ref(store, run_id)
    assert ref is not None, 'transcript.jsonl was not stored'
    return [json.loads(line) for line in sink.artifacts.read(ref).decode().splitlines()]


# --- 1. off by default ---

def test_transcript_is_off_by_default(store):
    run, _ = run_with(store, Scripted(FINAL), record=False)
    assert [a.name for a in store.artifacts(run.id)] == ['output.md']
    assert not any(e.type.startswith('transcript.') for e in run.events)


# --- 2. record order and documented fields ---

def test_transcript_records_follow_the_documented_order_and_fields(store):
    run, sink = run_with(store, Scripted(TOOL_TURN, FINAL), record=True, tools=Lookup())
    assert run.status == 'succeeded'
    recs = transcript(store, sink, run.id)
    assert [r['type'] for r in recs] == ['system', 'user', 'assistant', 'tool', 'assistant', 'end']

    system, user, first, tool, second, end = recs
    assert system == {'type': 'system', 'text': 'You are an analyst.'}
    assert user == {'type': 'user', 'text': INSTRUCTION}

    assert set(first) == ASSISTANT_KEYS
    assert (first['turn'], first['stop_reason'], first['text'], first['reasoning']) == (
        1, 'tool_use', '', 'Thinking it over.')
    assert first['tool_calls'] == [{'id': 'c1', 'name': 'lookup', 'arguments': {'key': 'k1'}}]
    assert set(first['usage']) == USAGE_KEYS
    assert first['usage'] == {'input_tokens': 100, 'output_tokens': 10, 'cache_read_tokens': 0,
                              'cache_write_tokens': 0, 'reasoning_tokens': 4, 'cost_usd': 0.001}
    assert (first['provider'], first['response_id'], first['model']) == ('Parasail', 'gen-7', 'm-x')
    assert isinstance(first['latency_ms'], int)

    assert set(tool) == TOOL_KEYS
    assert (tool['turn'], tool['id'], tool['name'], tool['arguments'], tool['content'], tool['is_error']) == (
        1, 'c1', 'lookup', {'key': 'k1'}, 'RESULT-TEXT', False)
    assert isinstance(tool['duration_ms'], int)

    assert set(second) == ASSISTANT_KEYS
    assert (second['turn'], second['text'], second['tool_calls']) == (2, 'FINAL-TEXT', [])
    assert (second['provider'], second['response_id'], second['model']) == (None, None, None)
    assert second['usage']['cost_usd'] is None

    assert set(end) == END_KEYS
    assert (end['status'], end['terminal_reason']) == ('succeeded', 'completed')
    assert all(isinstance(r['t'], (int, float)) for r in (first, tool, second, end))


def test_transcript_is_stored_with_a_recorded_event(store):
    run, _ = run_with(store, Scripted(FINAL), record=True)
    ref = transcript_ref(store, run.id)
    event = next(e.payload for e in run.events if e.type == 'transcript.recorded')
    assert event == {'records': 4, 'bytes': ref.size, 'truncated': False}
    assert ref.mime == 'text/plain'
    assert [e.type for e in run.events].index('transcript.recorded') < [e.type for e in run.events].index('run.manifest')


# --- 3. the end record is written on every path ---

def test_end_record_is_written_when_the_model_call_fails(store):
    run, sink = run_with(store, Scripted(TimeoutError('secret prompt echo')), record=True)
    assert run.terminal_reason == 'model_error:TimeoutError'
    recs = transcript(store, sink, run.id)
    assert [r['type'] for r in recs] == ['system', 'user', 'end']  # no assistant line for a failed call
    assert (recs[-1]['status'], recs[-1]['terminal_reason']) == ('failed', 'model_error:TimeoutError')
    assert 'secret prompt echo' not in json.dumps(recs)


def test_end_record_reports_an_exhausted_budget(store):
    budget = Budget(max_tokens=200_000, max_turns=1)  # one turn, then turns_exhausted
    d = definition()
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=INSTRUCTION)
    run = PreparedRun(run_id, d, budget, store.get(run_id).reserved_tokens, INSTRUCTION, Lookup(),
                      record_transcript=True)
    sink = LedgerSink(store, run_id)
    execute(sink, run, Scripted(TOOL_TURN, FINAL))
    recs = transcript(store, sink, run_id)
    assert recs[-1]['type'] == 'end' and recs[-1]['terminal_reason'] == 'turns_exhausted'


# --- 4. truncation and best-effort storage ---

def test_oversized_transcript_is_shortened_and_marked(store, monkeypatch):
    monkeypatch.setattr(harness, 'MAX_ARTIFACT_BYTES', 1500)
    big = ModelResponse(text='x' * 5000, tool_calls=(), stop_reason='end_turn', input_tokens=10, output_tokens=5,
                        reasoning='r' * 5000)
    run, sink = run_with(store, Scripted(big), record=True, instruction='y' * 5000)
    assert run.status == 'succeeded'  # the run's own output is untouched
    event = next(e.payload for e in run.events if e.type == 'transcript.recorded')
    assert event['truncated'] is True
    assert event['bytes'] <= 1500 and transcript_ref(store, run.id).size == event['bytes']

    raw = sink.artifacts.read(transcript_ref(store, run.id)).decode()
    recs = [json.loads(line) for line in raw.splitlines()]  # every line is still one JSON object
    assert [r['type'] for r in recs] == ['system', 'user', 'assistant', 'end']
    assert '…[truncated' in recs[1]['text'] and '…[truncated' in recs[2]['reasoning']
    assert recs[-1]['status'] == 'succeeded'


def test_a_rejected_transcript_upload_is_an_event_not_a_failed_run(store):
    class RejectTranscript(LedgerSink):
        def upload(self, name, mime, data):
            if name == 'transcript.jsonl':
                raise ArtifactRejected('too_large')
            return super().upload(name, mime, data)

    run, _ = run_with(store, Scripted(FINAL), record=True, sink_cls=RejectTranscript)
    assert run.status == 'succeeded'
    assert transcript_ref(store, run.id) is None
    assert next(e.payload for e in run.events if e.type == 'transcript.failed') == {'code': 'too_large'}


def test_a_transcript_that_cannot_be_shortened_still_parses_and_ends_with_end():
    records = [{'type': 'assistant', 'turn': i, 'text': 't' * 50} for i in range(1, 60)]
    records.append({'type': 'end', 't': 1.0, 'status': 'failed', 'terminal_reason': 'output_truncated'})
    data, truncated = harness._encode_transcript(records, 300)
    assert truncated is True and len(data) <= 300
    parsed = [json.loads(line) for line in data.decode().splitlines()]  # raises on a partial last line
    assert parsed[-1]['type'] == 'end'


# --- 5. the worker's MAEF_RECORD_TRANSCRIPT switch ---

def worker_run(store, monkeypatch, value):
    if value is None:
        monkeypatch.delenv('MAEF_RECORD_TRANSCRIPT', raising=False)
    else:
        monkeypatch.setenv('MAEF_RECORD_TRANSCRIPT', value)
    d = definition()
    d = d.model_copy(update={'definition_digest': compute_definition_digest(d.definition_bytes, d.files, d.mcp_bytes)})
    budget = Budget(max_tokens=200_000, max_turns=5)
    run_id = store.admit(d, budget, prompt_bytes=1000, instruction=INSTRUCTION)
    adapter = FakeAdapter(response=FINAL)
    Worker(LocalChannel(store), lambda profile: adapter, worker_id='w1').run_once()
    return store.get(run_id)


@pytest.mark.parametrize('value', ['1', 'true', 'YES'])
def test_worker_records_a_transcript_when_the_env_var_is_on(store, monkeypatch, value):
    run = worker_run(store, monkeypatch, value)
    assert run.status == 'succeeded'
    assert transcript_ref(store, run.id) is not None
    assert any(e.type == 'transcript.recorded' for e in run.events)


@pytest.mark.parametrize('value', [None, '0', '', 'no', 'off'])
def test_worker_records_no_transcript_unless_the_env_var_is_on(store, monkeypatch, value):
    run = worker_run(store, monkeypatch, value)
    assert run.status == 'succeeded'
    assert transcript_ref(store, run.id) is None
    assert not any(e.type.startswith('transcript.') for e in run.events)
