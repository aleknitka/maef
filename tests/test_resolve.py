import shutil
import pytest
from maef_api.registry import AgentRegistry, ModelProfileRegistry, PriceScheduleRegistry
from maef_api.resolve import ResolveError, resolve_definition
from maef_api.models import Budget
from test_api import PROFILE

SCHEDULE = """schema_version: maef.dev/v1alpha1
id: test-prices
effective_from: 2026-01-01
input_usd_per_million_tokens: "1.5"
output_usd_per_million_tokens: "7.5"
"""

@pytest.fixture
def env(tmp_path):
    agents = tmp_path / 'agents'
    shutil.copytree(AgentRegistry().root / 'research-analyst', agents / 'research-analyst')
    profiles = tmp_path / 'profiles'; profiles.mkdir()
    schedules = tmp_path / 'prices'; schedules.mkdir()
    (profiles / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra=''))
    return agents, profiles, schedules

def resolve(env):
    agents, profiles, schedules = env
    return resolve_definition('research-analyst', AgentRegistry(agents), ModelProfileRegistry(profiles), PriceScheduleRegistry(schedules))

def test_digest_stable_and_content_sensitive(env):
    first = resolve(env)
    assert resolve(env).definition_digest == first.definition_digest
    (env[0] / 'research-analyst' / 'SYSTEM.md').write_text('changed')
    assert resolve(env).definition_digest != first.definition_digest

def test_profile_digest_changes(env):
    first = resolve(env)
    (env[1] / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra='max_retries: 5\n'))
    assert resolve(env).profile_digest != first.profile_digest

def test_missing_profile_is_clear_error(env):
    (env[1] / 'local-default.yaml').unlink()
    with pytest.raises(ResolveError, match='local-default'):
        resolve(env)

def test_unpriced_profile_resolves_to_none(env):
    assert resolve(env).price_schedule is None

def test_priced_profile_resolves_schedule(env):
    (env[1] / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra='price_schedule_id: test-prices\n'))
    (env[2] / 'test-prices.yaml').write_text(SCHEDULE)
    schedule = resolve(env).price_schedule
    from decimal import Decimal
    assert schedule.output_usd_per_million_tokens == Decimal('7.5')

def test_outside_git_counts_as_dirty(env):
    # tmp_path is not a git repository, so the pin cannot claim a clean revision.
    resolved = resolve(env)
    assert resolved.git_rev is None and resolved.dirty is True

def test_budget_is_decimal():
    from decimal import Decimal
    assert isinstance(Budget().max_cost_usd, Decimal)
