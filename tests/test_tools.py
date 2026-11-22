import shutil
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from maef_api.definitions import Definitions, ResolveError
from maef_api.registry import AgentRegistry, ModelProfileRegistry
from maef_api.tools import MAX_RESULT_BYTES, BuiltinTools, ToolResult
from test_api import PROFILE

PINNED = {'SYSTEM.md': b'pinned instructions', 'skills/cite.md': b'cite sources'}


def test_specs_only_named_tools_in_order():
    tools = BuiltinTools(['read_agent_file', 'current_time'], PINNED)
    assert [s.name for s in tools.specs()] == ['read_agent_file', 'current_time']
    assert [s.name for s in BuiltinTools(['current_time'], PINNED).specs()] == ['current_time']


def test_current_time_uses_injected_clock():
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    result = BuiltinTools(['current_time'], {}, clock=lambda: when).invoke('current_time', {})
    assert result == ToolResult('2026-01-02T03:04:05+00:00')


def test_read_agent_file_returns_pinned_bytes():
    result = BuiltinTools(['read_agent_file'], PINNED).invoke('read_agent_file', {'path': 'SYSTEM.md'})
    assert result == ToolResult('pinned instructions', False)
    assert BuiltinTools(['read_agent_file'], PINNED).invoke('read_agent_file', {'path': 'skills/cite.md'}).content == 'cite sources'


@pytest.mark.parametrize('arguments', [
    {'path': 'notes.md'},       # not listed
    {'path': '../SYSTEM.md'},   # traversal
    {'path': '/etc/passwd'},    # absolute
    {'path': 5},                # non-string
    {'path': None},
    {},                         # missing
])
def test_read_agent_file_refuses(arguments):
    result = BuiltinTools(['read_agent_file'], PINNED).invoke('read_agent_file', arguments)
    assert result.is_error is True


def test_large_results_truncated_with_marker():
    tools = BuiltinTools(['read_agent_file'], {'big.md': b'a' * (MAX_RESULT_BYTES + 5000)})
    result = tools.invoke('read_agent_file', {'path': 'big.md'})
    assert result.content.endswith('\n[truncated]')
    assert len(result.content.encode()) <= MAX_RESULT_BYTES + len('\n[truncated]')
    assert result.is_error is False


def test_unknown_tool_name_at_invoke_is_error():
    result = BuiltinTools(['current_time'], {}).invoke('shell', {})
    assert result.is_error is True


def test_unknown_builtin_at_construction_raises():
    with pytest.raises(ValueError, match='shell'):
        BuiltinTools(['current_time', 'shell'], {})


@pytest.mark.parametrize('arguments', [None, [1, 2], 'SYSTEM.md', 42])
def test_invoke_never_raises_on_garbage_arguments(arguments):
    tools = BuiltinTools(['current_time', 'read_agent_file'], PINNED)
    for name in ('current_time', 'read_agent_file'):
        assert isinstance(tools.invoke(name, arguments), ToolResult)
    assert tools.invoke('read_agent_file', arguments).is_error is True


def test_invoke_turns_clock_failure_into_error_result():
    def boom():
        raise RuntimeError('clock broke')
    result = BuiltinTools(['current_time'], {}, clock=boom).invoke('current_time', {})
    assert result == ToolResult('tool_failed', True)


# --- Definitions: tool names and pinned files ---

@pytest.fixture
def agent_dir(tmp_path):
    agents = tmp_path / 'agents'
    shutil.copytree(AgentRegistry().root / 'research-analyst', agents / 'research-analyst')
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    (profiles / 'local-default.yaml').write_text(PROFILE.format(id='local-default', extra=''))
    return SimpleNamespace(agents=agents, profiles=profiles, agent=agents / 'research-analyst')


def resolve(env):
    return Definitions(AgentRegistry(env.agents), ModelProfileRegistry(env.profiles)).resolve('research-analyst')


def test_resolve_rejects_unknown_tool(agent_dir):
    yaml_path = agent_dir.agent / 'agent.yaml'
    yaml_path.write_text(yaml_path.read_text() + 'tools: [current_time, shell]\n')
    with pytest.raises(ResolveError, match='shell'):
        resolve(agent_dir)


def test_pinned_files_hold_instructions_and_skills_not_agent_yaml(agent_dir):
    (agent_dir.agent / 'skills').mkdir()
    (agent_dir.agent / 'skills' / 'cite.md').write_bytes(b'cite sources')
    yaml_path = agent_dir.agent / 'agent.yaml'
    yaml_path.write_text(yaml_path.read_text().replace('skills: []', 'skills: [skills/cite.md]'))
    pinned = resolve(agent_dir)
    assert pinned.files == {'SYSTEM.md': (agent_dir.agent / 'SYSTEM.md').read_bytes(), 'skills/cite.md': b'cite sources'}
    assert 'agent.yaml' not in pinned.files
