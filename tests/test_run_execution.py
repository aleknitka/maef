import json
import time
from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from maef_api import main
from maef_api.adapters.base import ModelResponse
from maef_api.adapters.bedrock import BedrockAdapter
from maef_api.admission import admit
from maef_api.harness import PreparedRun, execute
from maef_api.models import (
    Budget,
    ModelEndpointProfile,
    PriceSchedule,
    ResolvedDefinition,
)
from maef_api.runner import Runner
from maef_api.store import Store
from test_api import PROFILE


def profile(extra=''):
    import yaml
    return ModelEndpointProfile.model_validate(yaml.safe_load(PROFILE.format(id='test-model', extra=extra)))

def definition(dirty=False):
    price = PriceSchedule(schema_version='maef.dev/v1alpha1', id='test-prices', effective_from=date(2026, 1, 1),
                          input_usd_per_million_tokens=Decimal(1), output_usd_per_million_tokens=Decimal(5))
    return ResolvedDefinition(agent_id='research-analyst', agent_version='0.1.0', git_rev='abc', dirty=dirty,
                              definition_digest='d', model_profile_id='test-model', profile_digest='p', price_schedule=price)

class FakeAdapter:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []
    def converse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.response

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'l.db'); s.ensure_account('default', 1_000_000); return s

def prepared(store, **kw):
    d, p = definition(), profile()
    budget = Budget(max_cost_usd=Decimal('0.5'), max_turns=1, **kw)
    run_id = admit(store, d, budget, instruction='Question?', system_prompt_bytes=20, max_output_tokens=p.max_output_tokens)
    return PreparedRun(run_id, d, p, budget, store.get_run(run_id)['reserved_micro'], 'You are an analyst.', 'Question?')

def types(store, run_id):
    return [e['type'] for e in store.get_run(run_id)['events']]

def test_successful_run(store):
    run = prepared(store)
    adapter = FakeAdapter(ModelResponse('The answer.', 'end_turn', 1000, 200))
    execute(store, run, adapter)
    detail = store.get_run(run.run_id)
    assert detail['status'] == 'succeeded' and detail['cost_micro'] == 2000  # 1000*$1/M + 200*$5/M
    assert types(store, run.run_id) == ['task.created', 'task.admitted', 'run.started', 'model.request',
                                        'model.response', 'usage.recorded', 'run.output', 'run.manifest', 'task.succeeded']
    assert adapter.calls[0]['max_output_tokens'] == 4096
    manifest = next(e for e in detail['events'] if e['type'] == 'run.manifest')['payload']
    assert manifest['agent_revision'] == 'abc' and manifest['cost_micro_usd'] == 2000
    acct = store.account('default')
    assert (acct['reserved_micro'], acct['spent_micro']) == (0, 2000)

def test_prompts_not_logged(store):
    run = prepared(store)
    execute(store, run, FakeAdapter(ModelResponse('ok', 'end_turn', 10, 1)))
    dump = json.dumps([e for e in store.get_run(run.run_id)['events'] if e['type'] != 'run.output'])
    assert 'Question?' not in dump and 'You are an analyst' not in dump

def test_truncated_output_fails_but_is_kept(store):
    run = prepared(store)
    execute(store, run, FakeAdapter(ModelResponse('partial', 'max_tokens', 10, 4096)))
    detail = store.get_run(run.run_id)
    assert detail['status'] == 'failed' and detail['terminal_reason'] == 'output_truncated'
    assert 'run.output' in types(store, run.run_id)

def test_adapter_error_records_class_only_and_releases(store):
    run = prepared(store)
    execute(store, run, FakeAdapter(error=TimeoutError('secret prompt echo')))
    detail = store.get_run(run.run_id)
    assert detail['terminal_reason'] == 'model_error:TimeoutError' and detail['cost_micro'] == 0
    assert 'secret prompt echo' not in json.dumps(detail['events'])
    assert store.account('default')['reserved_micro'] == 0

def test_duration_exhausted_skips_model_call(store):
    run = prepared(store)
    ticks = iter([0, 10_000, 10_000])
    adapter = FakeAdapter(ModelResponse('x', 'end_turn', 1, 1))
    execute(store, run, adapter, clock=lambda: next(ticks))
    assert adapter.calls == [] and store.get_run(run.run_id)['terminal_reason'] == 'duration_exhausted'

def test_sweep_fails_orphans_closed(store):
    run = prepared(store)
    assert Runner(store).sweep_orphans() == 1
    detail = store.get_run(run.run_id)
    assert detail['terminal_reason'] == 'worker_lost' and detail['cost_micro'] == run.reserved_micro
    assert store.account('default')['spent_micro'] == run.reserved_micro

def test_bedrock_request_shape_and_usage_parsing():
    client = boto3.client('bedrock-runtime', region_name='eu-north-1', aws_access_key_id='x', aws_secret_access_key='y')
    adapter = BedrockAdapter(profile(), client=client)
    with Stubber(client) as stub:
        stub.add_response('converse', {
            'output': {'message': {'role': 'assistant', 'content': [{'text': 'Hi '}, {'text': 'there'}]}},
            'stopReason': 'end_turn', 'usage': {'inputTokens': 12, 'outputTokens': 3, 'totalTokens': 15},
            'metrics': {'latencyMs': 1},
        }, expected_params={
            'modelId': 'global.test.model-1', 'system': [{'text': 'sys'}],
            'messages': [{'role': 'user', 'content': [{'text': 'q'}]}], 'inferenceConfig': {'maxTokens': 256},
        })
        out = adapter.converse(system='sys', user='q', max_output_tokens=256, timeout_seconds=30)
    assert (out.text, out.stop_reason, out.input_tokens, out.output_tokens) == ('Hi there', 'end_turn', 12, 3)

def test_bedrock_rejects_other_dialects_and_credentials():
    with pytest.raises(ValueError):
        BedrockAdapter(profile().model_copy(update={'credential_ref': 'env:aws-keys'}))

# --- HTTP ---
@pytest.fixture
def api(tmp_path, monkeypatch):
    s = Store(tmp_path / 'api.db'); s.ensure_account('default', 5_000_000)
    monkeypatch.setattr(main, 'store', s)
    monkeypatch.setattr(main, 'runner', Runner(s))
    monkeypatch.setenv('MAEF_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('MAEF_ALLOW_DIRTY', '1')
    monkeypatch.setattr(main, 'resolve_definition', lambda *a, **k: definition(dirty=True))
    monkeypatch.setattr(main.model_profiles, 'get_profile', lambda _id: profile())
    adapter = FakeAdapter(ModelResponse('Done.', 'end_turn', 100, 10))
    monkeypatch.setattr(main, 'adapter_factory', lambda p: adapter)
    return TestClient(main.app), s

def test_post_runs_executes_in_background(api):
    client, _ = api
    res = client.post('/api/v1/runs', json={'agent_id': 'research-analyst', 'instruction': 'Hello'})
    assert res.status_code == 202
    run_id = res.json()['run_id']
    for _ in range(100):
        if client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'succeeded':
            break
        time.sleep(0.05)
    assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'succeeded'

def test_post_runs_disabled_flag_and_health(api, monkeypatch):
    client, _ = api
    assert client.get('/health').json()['execution_enabled'] is True
    monkeypatch.delenv('MAEF_EXECUTION_ENABLED')
    assert client.get('/health').json()['execution_enabled'] is False
    assert client.post('/api/v1/runs', json={'agent_id': 'research-analyst', 'instruction': 'x'}).status_code == 501

def test_post_runs_refuses_dirty_without_flag(api, monkeypatch):
    client, s = api
    monkeypatch.delenv('MAEF_ALLOW_DIRTY')
    res = client.post('/api/v1/runs', json={'agent_id': 'research-analyst', 'instruction': 'x'})
    assert res.status_code == 409 and res.json()['detail']['code'] == 'dirty' and s.list_runs() == []
