import shutil

import pytest
from maef_api.models import Budget
from maef_api.registry import AgentRegistry, ModelProfileRegistry
from maef_api.resolve import ResolveError, resolve_definition
from test_api import PROFILE


@pytest.fixture
def env(tmp_path):
    agents = tmp_path / 'agents'
    shutil.copytree(AgentRegistry().root / 'research-analyst', agents / 'research-analyst')
    profiles = tmp_path / 'profiles'; profiles.mkdir()
    (profiles / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra=''))
    return agents, profiles

def resolve(env):
    agents, profiles = env
    return resolve_definition('research-analyst', AgentRegistry(agents), ModelProfileRegistry(profiles))

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

def test_outside_git_counts_as_dirty(env):
    # tmp_path is not a git repository, so the pin cannot claim a clean revision.
    resolved = resolve(env)
    assert resolved.git_rev is None and resolved.dirty is True

def test_budget_is_token_based():
    assert Budget().max_tokens == 100_000
