from types import SimpleNamespace

import pytest

from conftest import copy_research_analyst
from maef_api.adapters.base import ModelResponse
from maef_api.channel import LocalChannel
from maef_api.definitions import Definitions
from maef_api.ledger import Ledger
from maef_api.models import Budget, BudgetOverride, RunRequest
from maef_api.registry import AgentRegistry, ModelProfileRegistry
from maef_api.runs import (
    AdmissionRejected,
    AgentNotFound,
    DefinitionConflict,
    ExecutionDisabled,
    Runs,
    UnsupportedDialect,
)
from maef_api.worker import Worker
from test_api import PROFILE


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(text="answer", tool_calls=(), stop_reason="end_turn", input_tokens=7, output_tokens=5)


@pytest.fixture
def env(tmp_path):
    agents = tmp_path / "agents"
    copy_research_analyst(agents)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "local-default.yaml").write_text(PROFILE.format(id="local-default", extra=""))
    ledger = Ledger(tmp_path / "l.db")
    ledger.ensure_account("default", 5_000_000)
    return SimpleNamespace(agents=agents, profiles=profiles, ledger=ledger, adapter=FakeAdapter())


def make_runs(env, *, enabled=True, dirty_ok=True, **kw):
    definitions = Definitions(AgentRegistry(env.agents), ModelProfileRegistry(env.profiles))
    return Runs(definitions, env.ledger, execution_enabled=lambda: enabled, allow_dirty=lambda: dirty_ok, **kw)


def run_worker(env):
    return Worker(LocalChannel(env.ledger), lambda profile: env.adapter, worker_id="w1").run_once()


def request(budget=None, agent_id="research-analyst"):
    return RunRequest(agent_id=agent_id, instruction="Summarise", budget=budget)


def test_preview_clamps_budget_and_reports_profile(env):
    preview = make_runs(env).preview(
        request(BudgetOverride(max_tokens=50_000, max_duration_seconds=5_000, max_turns=10))
    )
    # Agent ceiling is 200k tokens, 900 s, 30 turns: a lower request is kept, a higher one is clamped.
    assert preview.effective_budget == Budget(max_tokens=50_000, max_duration_seconds=900, max_turns=10)
    assert preview.model_profile == "local-default"


def test_submit_refused_when_execution_disabled_writes_nothing(env):
    with pytest.raises(ExecutionDisabled):
        make_runs(env, enabled=False).submit(request())
    assert env.ledger.list() == []


def test_submit_unknown_agent(env):
    with pytest.raises(AgentNotFound):
        make_runs(env).submit(request(agent_id="no-such-agent"))


def test_submit_missing_profile_is_definition_conflict(env):
    (env.profiles / "local-default.yaml").unlink()
    with pytest.raises(DefinitionConflict, match="local-default"):
        make_runs(env).submit(request())


def test_submit_dirty_refused_unless_allowed(env):
    with pytest.raises(AdmissionRejected) as e:
        make_runs(env, dirty_ok=False).submit(request())
    assert e.value.code == "dirty"


def test_unsupported_dialect_refused_before_admission(env):
    with pytest.raises(UnsupportedDialect):
        make_runs(env, supported_dialects=("openai-compatible",)).submit(request())
    assert env.ledger.list() == []


def test_submit_queues_and_worker_completes(env):
    runs = make_runs(env)
    record = runs.submit(request())
    assert record.status == "admitted" and env.adapter.calls == []  # submit never executes
    assert run_worker(env) is True and run_worker(env) is False
    detail = runs.get(record.id)
    assert detail.status == "succeeded" and detail.terminal_reason == "completed"
    assert detail.tokens_used == 7 + 5


def test_system_prompt_is_the_pinned_instructions(env):
    make_runs(env).submit(request())
    run_worker(env)
    system_md = (env.agents / "research-analyst" / "SYSTEM.md").read_text()
    assert env.adapter.calls[0]["system"] == system_md


def test_recover_fails_expired_leases_and_keeps_queued_runs(env):
    now = [1000.0]
    runs = make_runs(env, clock=lambda: now[0])
    leased_id = runs.submit(request()).id
    queued_id = runs.submit(request()).id
    assert env.ledger.claim("w1", 60, now[0]).run_id == leased_id
    assert runs.recover() == 0
    now[0] = 2000.0
    assert runs.recover() == 1
    detail = runs.get(leased_id)
    assert detail.status == "failed" and detail.terminal_reason == "worker_lost"
    assert runs.get(queued_id).status == "admitted"
    assert runs.recover() == 0


def test_partial_budget_override_keeps_other_ceilings():
    from maef_api.models import Budget, BudgetOverride
    from maef_api.runs import effective_budget

    ceiling = Budget(max_tokens=200_000, max_duration_seconds=900, max_turns=30)
    out = effective_budget(ceiling, BudgetOverride(max_tokens=50_000))
    assert (out.max_tokens, out.max_duration_seconds, out.max_turns) == (50_000, 900, 30)
    assert effective_budget(ceiling, BudgetOverride(max_turns=99)).max_turns == 30
