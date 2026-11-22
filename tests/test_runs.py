import shutil
from types import SimpleNamespace

import pytest
from maef_api.adapters.base import ModelResponse
from maef_api.definitions import Definitions
from maef_api.ledger import Ledger
from maef_api.models import Budget, RunRequest
from maef_api.registry import AgentRegistry, ModelProfileRegistry
from maef_api.runs import (
    AdmissionRejected,
    AgentNotFound,
    DefinitionConflict,
    ExecutionDisabled,
    Runs,
    UnsupportedDialect,
)
from test_api import PROFILE


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(text='answer', stop_reason='end_turn', input_tokens=7, output_tokens=5)


def inline(fn):
    fn()


@pytest.fixture
def env(tmp_path):
    agents = tmp_path / 'agents'
    shutil.copytree(AgentRegistry().root / 'research-analyst', agents / 'research-analyst')
    profiles = tmp_path / 'profiles'; profiles.mkdir()
    (profiles / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra=''))
    ledger = Ledger(tmp_path / 'l.db')
    ledger.ensure_account('default', 5_000_000)
    return SimpleNamespace(agents=agents, profiles=profiles, ledger=ledger, adapter=FakeAdapter())


def make_runs(env, *, executor=inline, factory=None, enabled=True, dirty_ok=True):
    factory = factory or (lambda profile: env.adapter)
    definitions = Definitions(AgentRegistry(env.agents), ModelProfileRegistry(env.profiles))
    return Runs(definitions, env.ledger, factory, executor,
                execution_enabled=lambda: enabled, allow_dirty=lambda: dirty_ok)


def request(budget=None, agent_id='research-analyst'):
    return RunRequest(agent_id=agent_id, instruction='Summarise', budget=budget)


def test_preview_clamps_budget_and_reports_profile(env):
    preview = make_runs(env).preview(request(Budget(max_tokens=50_000, max_duration_seconds=5_000, max_turns=10)))
    # Agent ceiling is 200k tokens, 900 s, 30 turns: a lower request is kept, a higher one is clamped.
    assert preview.effective_budget == Budget(max_tokens=50_000, max_duration_seconds=900, max_turns=10)
    assert preview.model_profile == 'local-default'


def test_submit_refused_when_execution_disabled_writes_nothing(env):
    with pytest.raises(ExecutionDisabled):
        make_runs(env, enabled=False).submit(request())
    assert env.ledger.list() == []


def test_submit_unknown_agent(env):
    with pytest.raises(AgentNotFound):
        make_runs(env).submit(request(agent_id='no-such-agent'))


def test_submit_missing_profile_is_definition_conflict(env):
    (env.profiles / 'local-default.yaml').unlink()
    with pytest.raises(DefinitionConflict, match='local-default'):
        make_runs(env).submit(request())


def test_submit_dirty_refused_unless_allowed(env):
    with pytest.raises(AdmissionRejected) as e:
        make_runs(env, dirty_ok=False).submit(request())
    assert e.value.code == 'dirty'


def test_adapter_factory_unsupported_dialect_propagates(env):
    def factory(profile):
        raise UnsupportedDialect('no adapter')
    with pytest.raises(UnsupportedDialect):
        make_runs(env, factory=factory).submit(request())
    assert env.ledger.list() == []


def test_successful_run_completes_inline(env):
    runs = make_runs(env)
    record = runs.submit(request())
    detail = runs.get(record.id)
    assert detail.status == 'succeeded' and detail.terminal_reason == 'completed'
    assert record.status == 'succeeded'  # submit returns post-executor state
    assert detail.tokens_used == 7 + 5


def test_system_prompt_is_the_pinned_instructions(env):
    make_runs(env).submit(request())
    system_md = (env.agents / 'research-analyst' / 'SYSTEM.md').read_text()
    assert env.adapter.calls[0]['system'] == system_md


def test_recover_fails_orphans_with_worker_lost(env):
    orphaned = make_runs(env, executor=lambda fn: None)  # never runs the function
    run_id = orphaned.submit(request()).id
    runs = make_runs(env)
    assert runs.recover() == 1
    detail = runs.get(run_id)
    assert detail.status == 'failed' and detail.terminal_reason == 'worker_lost'
    assert runs.recover() == 0
