import pytest
from conftest import copy_research_analyst
from maef_api.definitions import Definitions, ResolveError
from maef_api.models import Budget
from maef_api.registry import AgentRegistry, ModelProfileRegistry
from test_api import PROFILE


@pytest.fixture
def env(tmp_path):
    agents = tmp_path / "agents"
    copy_research_analyst(agents)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "local-default.yaml").write_text(PROFILE.format(id="local-default", extra=""))
    return agents, profiles


def resolve(env):
    agents, profiles = env
    return Definitions(AgentRegistry(agents), ModelProfileRegistry(profiles)).resolve("research-analyst")


def test_digest_stable_and_content_sensitive(env):
    first = resolve(env)
    assert resolve(env).definition_digest == first.definition_digest
    (env[0] / "research-analyst" / "SYSTEM.md").write_text("changed")
    assert resolve(env).definition_digest != first.definition_digest


def test_profile_digest_changes(env):
    first = resolve(env)
    (env[1] / "local-default.yaml").write_text(PROFILE.format(id="local-default", extra="max_retries: 5\n"))
    assert resolve(env).profile_digest != first.profile_digest


def test_missing_profile_is_clear_error(env):
    (env[1] / "local-default.yaml").unlink()
    with pytest.raises(ResolveError, match="local-default"):
        resolve(env)


def test_outside_git_counts_as_dirty(env):
    # tmp_path is not a git repository, so the pin cannot claim a clean revision.
    resolved = resolve(env)
    assert resolved.git_rev is None and resolved.dirty is True


def test_budget_is_token_based():
    assert Budget().max_tokens == 100_000


def test_instructions_come_from_digested_bytes(env):
    (env[0] / "research-analyst" / "SYSTEM.md").write_text("pinned prompt")
    assert resolve(env).instructions == "pinned prompt"


def test_sandbox_profile_is_pinned_when_available(env):
    from maef_api.registry import SandboxProfileRegistry

    agents, profiles = env
    sandboxes = AgentRegistry().root.parent / "sandbox-profiles"
    pinned = Definitions(
        AgentRegistry(agents), ModelProfileRegistry(profiles), SandboxProfileRegistry(sandboxes)
    ).resolve("research-analyst")
    assert pinned.sandbox.id == "restricted-local" and len(pinned.sandbox_digest) == 64
    assert resolve(env).sandbox is None  # no registry, no sandboxed tools: optional
