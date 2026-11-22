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


# --- run_command / sandbox ---

from maef_api.models import SandboxProfile

SANDBOX_FOR_TOOLS = SandboxProfile(schema_version='maef.dev/v1alpha1', id='restricted-local',
                                   image='docker.io/library/busybox:1.37', cpus=1, memory_mb=64,
                                   pids_limit=32, workspace_mb=8)

import json

from maef_api.sandbox import (
    ExecResult,
    FakeSandbox,
    OciCliSandbox,
    SandboxUnavailable,
    workspace_path,
)


def test_run_command_requires_a_session():
    with pytest.raises(ValueError):
        BuiltinTools(['run_command'], {})


def test_run_command_returns_capped_json_and_validates_arguments():
    session = FakeSandbox(lambda argv, stdin: ExecResult(3, b'x' * 100_000, b'err')).provision(SANDBOX_FOR_TOOLS, 'r1')
    tools = BuiltinTools(['run_command'], {}, session=session)
    result = tools.invoke('run_command', {'argv': ['ls'], 'stdin': 'in'})
    body = json.loads(result.content)
    assert body['exit_code'] == 3 and body['stderr'] == 'err' and body['timed_out'] is False
    assert tools.invoke('run_command', {'argv': 'ls'}).is_error
    assert tools.invoke('run_command', {'argv': []}).is_error
    expired = BuiltinTools(['run_command'], {}, session=session, exec_timeout=lambda: 0)
    assert expired.invoke('run_command', {'argv': ['ls']}).is_error


def test_workspace_paths_are_confined():
    for bad in ('/etc/passwd', '/workspace', '/workspace/../etc', '/workspaceX/a', 'relative', '/workspace/a/../../x'):
        with pytest.raises(ValueError):
            workspace_path(bad)
    assert workspace_path('/workspace/a/b') == '/workspace/a/b'


def test_unpinned_image_and_rootless_fail_closed(monkeypatch):
    sb = OciCliSandbox('podman', '/nonexistent/podman', env={})
    with pytest.raises(SandboxUnavailable):  # runtime cannot be queried, so it is not rootless
        sb.provision(SANDBOX_FOR_TOOLS, 'r1')
    sb._info = {'runtime': 'podman', 'version': '5', 'rootless': True}
    with pytest.raises(SandboxUnavailable, match='unpinned_image'):
        sb.provision(SANDBOX_FOR_TOOLS, 'r1')


def test_agent_with_sandboxed_tool_needs_sandbox_profile(agent_dir):
    yaml_path = agent_dir.agent / 'agent.yaml'
    yaml_path.write_text(yaml_path.read_text() + 'tools: [run_command]\n')
    with pytest.raises(ResolveError, match='Sandbox profile'):
        resolve(agent_dir)


def test_oci_provision_builds_hardened_command_line(tmp_path):
    log = tmp_path / 'calls'
    fake = tmp_path / 'podman'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncase "$1" in info) echo true;; --version) echo "podman version 5.2.1";; esac\n')
    fake.chmod(0o755)
    sb = OciCliSandbox('podman', str(fake), env={'MAEF_ALLOW_UNPINNED_IMAGES': '1'})
    session = sb.provision(SANDBOX_FOR_TOOLS, 'run-abc')
    session.teardown()
    session.teardown()
    run = next(line for line in log.read_text().splitlines() if line.startswith('run '))
    for flag in ('--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--network=none',
                 '--pids-limit 32', '--memory 64m', '--user 65534:65534', '--label maef.run=run-abc',
                 '/workspace:rw,size=8m,mode=1777', '--entrypoint sleep'):
        assert flag in run
    assert '-v ' not in run and '--volume' not in run and '.sock' not in run
    assert sb.info() == {'runtime': 'podman', 'version': '5.2.1', 'rootless': True}
    assert log.read_text().count(f'rm -f {session.name}') == 1
