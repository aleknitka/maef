"""MCP end to end through public interfaces: the stdio client and tool set, manifests and definition
pinning, worker startup and teardown, the OCI spawn command line, and admission. Servers are
FakeMcpServer instances; the runtime is the fake podman CLI from test_sandbox_cli."""

import json

import pytest
import yaml
from maef_api.adapters.base import ModelResponse, ToolCall
from maef_api.channel import LocalChannel
from maef_api.credentials import Credentials
from maef_api.definitions import Definitions, ResolveError, compute_definition_digest
from maef_api.ledger import Ledger
from maef_api.mcp import (
    ADMISSION_ALLOWANCE_BYTES,
    MAX_DESCRIPTION_BYTES,
    CompositeToolSet,
    FakeMcpServer,
    McpToolSet,
)
from maef_api.models import AgentDefinition, Budget, McpServerManifest, RunRequest
from maef_api.registry import AgentRegistry, McpServerRegistry, ModelProfileRegistry
from maef_api.runs import Runs
from maef_api.sandbox import MAX_LINE_BYTES, FakeSandbox, SandboxError
from maef_api.tools import BuiltinTools, ToolResult
from maef_api.worker import Worker
from pydantic import ValidationError
from test_api import PROFILE
from test_run_execution import definition
from test_sandbox_cli import RUN_ID as CLI_RUN_ID
from test_sandbox_cli import calls, sandbox
from test_sandbox_cli import profile as cli_profile

IMAGE = "docker.io/mcp/fetch@sha256:" + "b" * 64
SECRET = "SECRET-VALUE-9f2c41"


def tool(name, **extra):
    return {"name": name, "description": f"{name} tool", "inputSchema": {"type": "object", "properties": {}}, **extra}


def mcp_manifest(server_id="fetch", **overrides):
    base = {
        "schema_version": "maef.dev/v1alpha1",
        "id": server_id,
        "version": "0.1.0",
        "transport": "stdio",
        "image": IMAGE,
        "command": [f"mcp-server-{server_id}"],
        "network": "open",
        "permission": "read-only",
    }
    return McpServerManifest(**{**base, **overrides})


# --- McpToolSet: handshake, listing, bounds ---


def test_start_performs_handshake_then_lists_tools():
    server = FakeMcpServer([tool("fetch")])
    McpToolSet("web", server).start()
    assert [m["method"] for m in server.received] == ["initialize", "notifications/initialized", "tools/list"]
    assert "id" not in server.received[1]


def test_tools_are_exposed_under_the_server_prefix():
    server = FakeMcpServer([tool("fetch"), tool("search")])
    tools = McpToolSet("web", server).start()
    assert [s.name for s in tools.specs()] == ["mcp__web__fetch", "mcp__web__search"]
    assert tools.server_of("mcp__web__fetch") == "web"


def test_list_pagination_is_followed_to_the_end():
    server = FakeMcpServer([tool(f"t{i}") for i in range(5)], page_size=2)
    tools = McpToolSet("web", server).start()
    assert len(tools.specs()) == 5 and tools.listed == 5 and tools.truncated is False
    cursors = [m.get("params", {}).get("cursor") for m in server.received if m["method"] == "tools/list"]
    assert cursors == [None, "2", "4"]


def test_more_than_max_tools_is_truncated():
    server = FakeMcpServer([tool(f"t{i}") for i in range(5)])
    tools = McpToolSet("web", server, max_tools=3).start()
    assert [s.name for s in tools.specs()] == ["mcp__web__t0", "mcp__web__t1", "mcp__web__t2"]
    assert tools.truncated is True and tools.listed == 5


def test_long_description_is_capped_and_control_characters_stripped():
    text = "ok\x1b[31m\u200bx\n" + "y" * 5000
    server = FakeMcpServer([tool("fetch", description=text)])
    spec = McpToolSet("web", server).start().specs()[0]
    assert len(spec.description.encode()) == MAX_DESCRIPTION_BYTES
    assert "\x1b" not in spec.description and "\u200b" not in spec.description
    assert spec.description.startswith("ok[31mx\n")


def test_tool_whose_input_schema_is_not_an_object_is_skipped():
    server = FakeMcpServer(
        [
            {"name": "bad-string", "inputSchema": "string"},
            {"name": "bad-list", "inputSchema": []},
            {"name": "missing"},
            tool("good"),
        ]
    )
    tools = McpToolSet("web", server).start()
    assert [s.name for s in tools.specs()] == ["mcp__web__good"]


# --- McpToolSet: invoke ---


def test_text_content_is_joined_and_other_items_rendered_as_omitted():
    def call(name, arguments):
        return {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "image", "data": "AAAA"},
                {"type": "text", "text": "second"},
            ]
        }

    tools = McpToolSet("web", FakeMcpServer([tool("fetch")], call=call)).start()
    assert tools.invoke("mcp__web__fetch", {"url": "https://example.com"}) == ToolResult(
        "first\n[image omitted]\nsecond", False
    )


def test_is_error_flag_is_carried_through():
    server = FakeMcpServer(
        [tool("fetch")], call=lambda n, a: {"content": [{"type": "text", "text": "boom"}], "isError": True}
    )
    result = McpToolSet("web", server).start().invoke("mcp__web__fetch", {})
    assert result == ToolResult("boom", True)


def test_silent_server_gives_a_timeout_result_and_does_not_raise():
    server = FakeMcpServer([tool("fetch")])
    tools = McpToolSet("web", server, timeout=lambda: 0.05).start()
    server.silent = True
    assert tools.invoke("mcp__web__fetch", {}) == ToolResult("mcp_error:timeout", True)


def test_unrelated_messages_are_skipped_while_waiting_for_the_answer():
    junk = [
        b"\n",
        b'{"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}\n',
        b'{"jsonrpc":"2.0","id":999,"result":{"content":[]}}\n',
        b'{"jsonrpc":"2.0","method":"ping","id":5}\n',
    ]
    server = FakeMcpServer([tool("fetch")], junk=junk)
    tools = McpToolSet("web", server).start()
    assert tools.invoke("mcp__web__fetch", {}) == ToolResult("ok", False)


@pytest.mark.parametrize("line", [b"this is not json\n", b"[1, 2, 3]\n"])
def test_non_jsonrpc_lines_are_skipped(line):
    server = FakeMcpServer([tool("fetch")])
    tools = McpToolSet("web", server).start()
    server.junk = [line]
    assert tools.invoke("mcp__web__fetch", {}) == ToolResult("ok", False)


def test_line_over_one_mib_is_an_error_result_not_an_exception():
    server = FakeMcpServer([tool("fetch")])
    tools = McpToolSet("web", server).start()
    server.junk = [b"x" * (MAX_LINE_BYTES + 1) + b"\n"]
    assert tools.invoke("mcp__web__fetch", {}) == ToolResult("mcp_error:line_too_long", True)


def test_unknown_tool_name_is_an_error_result():
    tools = McpToolSet("web", FakeMcpServer([tool("fetch")])).start()
    assert tools.invoke("mcp__web__nope", {}) == ToolResult("unknown_tool", True)


# --- CompositeToolSet ---


def echo_as(label):
    return lambda name, arguments: {"content": [{"type": "text", "text": f"{label}:{name}"}]}


def test_composite_merges_specs_and_routes_each_call_to_its_owner():
    alpha = McpToolSet("alpha", FakeMcpServer([tool("echo")], call=echo_as("a"))).start()
    beta = McpToolSet("beta", FakeMcpServer([tool("echo"), tool("ping")], call=echo_as("b"))).start()
    composite = CompositeToolSet([BuiltinTools(["current_time"], {}), alpha, beta])
    assert [s.name for s in composite.specs()] == [
        "current_time",
        "mcp__alpha__echo",
        "mcp__beta__echo",
        "mcp__beta__ping",
    ]
    assert composite.invoke("mcp__beta__echo", {}).content == "b:echo"
    assert composite.invoke("mcp__alpha__echo", {}).content == "a:echo"
    assert composite.invoke("current_time", {}).is_error is False


def test_duplicate_tool_names_are_a_construction_error():
    first = McpToolSet("alpha", FakeMcpServer([tool("echo")])).start()
    twin = McpToolSet("alpha", FakeMcpServer([tool("echo")])).start()
    with pytest.raises(ValueError, match="duplicate_tool_name:mcp__alpha__echo"):
        CompositeToolSet([first, twin])


def test_unknown_name_through_composite_is_an_error_result():
    composite = CompositeToolSet([McpToolSet("alpha", FakeMcpServer([tool("echo")])).start()])
    assert composite.invoke("mcp__gamma__nope", {}) == ToolResult("unknown_tool", True)


# --- manifests ---


@pytest.mark.parametrize("name", ["PATH", "LD_PRELOAD", "DOCKER_HOST"])
@pytest.mark.parametrize("field", ["env", "secrets"])
def test_manifest_rejects_protected_env_names(name, field):
    value = {"env": {name: "x"}, "secrets": {name: f"env:{name}"}}[field]
    with pytest.raises(ValidationError):
        mcp_manifest(**{field: value})


def test_manifest_accepts_an_ordinary_env_name():
    assert mcp_manifest(env={"FETCH_LOG_LEVEL": "info"}).env == {"FETCH_LOG_LEVEL": "info"}


# --- definitions: pinning MCP servers ---

AGENT_YAML = """schema_version: maef.dev/v1alpha1
id: research-analyst
version: 0.1.0
display_name: R
description: d
instructions: SYSTEM.md
mcp_servers: {refs}
permissions:
  network_open: {network_open}
runtime:
  model_profile: local-default
"""

FETCH_YAML = yaml.safe_dump(
    {
        "schema_version": "maef.dev/v1alpha1",
        "id": "fetch",
        "version": "0.1.0",
        "transport": "stdio",
        "image": IMAGE,
        "command": ["mcp-server-fetch"],
        "env": {},
        "secrets": {},
        "network": "open",
        "permission": "read-only",
        "max_tools": 8,
    }
)


def write_fixture(root, refs=("fetch@0.1.0",), network_open=True):
    """A throwaway definitions tree: one agent, its model profile and the mcp-servers directory."""
    agents, profiles, mcp = root / "agents", root / "profiles", root / "mcp-servers"
    (agents / "research-analyst").mkdir(parents=True)
    (agents / "research-analyst" / "agent.yaml").write_text(
        AGENT_YAML.format(refs=json.dumps(list(refs)), network_open=str(network_open).lower())
    )
    (agents / "research-analyst" / "SYSTEM.md").write_text("You are an analyst.")
    profiles.mkdir()
    (profiles / "local-default.yaml").write_text(PROFILE.format(id="local-default", extra=""))
    mcp.mkdir()
    (mcp / "fetch.yaml").write_text(FETCH_YAML)
    return agents, profiles, mcp


def resolve(paths):
    agents, profiles, mcp = paths
    return Definitions(AgentRegistry(agents), ModelProfileRegistry(profiles), mcps=McpServerRegistry(mcp)).resolve(
        "research-analyst"
    )


def test_resolve_pins_the_manifest_and_its_bytes(tmp_path):
    pinned = resolve(write_fixture(tmp_path))
    assert [m.id for m in pinned.mcp_servers] == ["fetch"]
    assert pinned.mcp_bytes["fetch"] == (tmp_path / "mcp-servers" / "fetch.yaml").read_bytes()


@pytest.mark.parametrize(
    "refs, network_open, match",
    [
        (("fetch@9.9.9",), True, "requires 9.9.9"),
        (("ghost@0.1.0",), True, "not found"),
        (("fetch@0.1.0", "fetch@0.1.0"), True, "listed twice"),
        (("fetch",), True, "must be"),
        (("fetch@0.1.0",), False, "network_open"),
    ],
)
def test_bad_mcp_references_and_permissions_are_resolve_errors(tmp_path, refs, network_open, match):
    with pytest.raises(ResolveError, match=match):
        resolve(write_fixture(tmp_path, refs=refs, network_open=network_open))


def test_digest_without_mcp_servers_is_the_plain_file_digest(tmp_path):
    pinned = resolve(write_fixture(tmp_path, refs=()))
    assert pinned.mcp_servers == [] and pinned.mcp_bytes == {}
    assert pinned.definition_digest == compute_definition_digest(pinned.definition_bytes, pinned.files)
    assert compute_definition_digest(b"x", {}, {}) == compute_definition_digest(b"x", {})


def test_digest_changes_when_a_referenced_manifest_byte_changes(tmp_path):
    paths = write_fixture(tmp_path)
    first = resolve(paths)
    assert resolve(paths).definition_digest == first.definition_digest
    manifest = paths[2] / "fetch.yaml"
    manifest.write_text(manifest.read_text() + "# same content, different bytes\n")
    assert resolve(paths).definition_digest != first.definition_digest


# --- worker: MCP startup, events and teardown ---


class ToolCallingAdapter:
    """Asks for `tool_name` on the first turn, then answers."""

    def __init__(self, tool_name):
        self.tool_name, self.calls = tool_name, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return ModelResponse(
                "", (ToolCall("c1", self.tool_name, {"url": "https://example.com"}),), "tool_use", 3, 2
            )
        return ModelResponse("done", (), "end_turn", 3, 2)


class TamperedMcpChannel(LocalChannel):
    """Hands the worker a lease whose pinned MCP bytes differ from what was admitted."""

    def claim(self, worker_id):
        lease = super().claim(worker_id)
        if lease is None:
            return None
        raw = {**lease.task.pinned.mcp_bytes, "fetch": lease.task.pinned.mcp_bytes["fetch"] + b"# tampered\n"}
        pinned = lease.task.pinned.model_copy(update={"mcp_bytes": raw})
        return lease.model_copy(update={"task": lease.task.model_copy(update={"pinned": pinned})})


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / "mcp.db")
    s.ensure_account("default", 1_000_000)
    return s


def mcp_pinned(*manifests, network_open=True):
    d = definition()
    agent = AgentDefinition(
        **{
            **d.agent.model_dump(),
            "mcp_servers": [f"{m.id}@{m.version}" for m in manifests],
            "permissions": {"network_open": network_open},
        }
    )
    raw = {m.id: yaml.safe_dump(m.model_dump(mode="json")).encode() for m in manifests}
    return d.model_copy(
        update={
            "agent": agent,
            "mcp_servers": list(manifests),
            "mcp_bytes": raw,
            "definition_digest": compute_definition_digest(d.definition_bytes, d.files, raw),
        }
    )


def admit_mcp(store, pinned):
    return store.admit(
        pinned,
        Budget(max_tokens=500_000, max_turns=2),
        prompt_bytes=28 + ADMISSION_ALLOWANCE_BYTES * len(pinned.mcp_servers),
        instruction="go",
    )


def spawner(made):
    def spawn(argv, env):
        server = FakeMcpServer([tool("fetch")])
        made.append(server)
        return server

    return spawn


def test_mcp_events_bracket_the_run_and_carry_the_server_id(store):
    run_id = admit_mcp(store, mcp_pinned(mcp_manifest()))
    made, adapter = [], ToolCallingAdapter("mcp__fetch__fetch")
    sandbox_ = FakeSandbox(spawn_factory=spawner(made))
    assert Worker(LocalChannel(store), lambda p: adapter, worker_id="w1", sandbox=sandbox_).run_once() is True

    detail = store.get(run_id)
    assert detail.status == "succeeded"
    types = [e.type for e in detail.events]
    provisioned = next(e.payload for e in detail.events if e.type == "sandbox.provisioned")
    assert provisioned["server"] == "fetch" and provisioned["network"] == "open"
    assert types.index("sandbox.provisioned") < types.index("mcp.started") < types.index("tool.call")
    tool_events = [e for e in detail.events if e.type in ("tool.call", "tool.result")]
    assert len(tool_events) == 2 and all(e.payload["server"] == "fetch" for e in tool_events)
    assert types[-2:] == ["mcp.stopped", "task.succeeded"]
    assert made[0].closed
    assert "mcp__fetch__fetch" in [s.name for s in adapter.calls[0]["tools"]]


def test_missing_secret_fails_before_any_model_call(store):
    manifest = mcp_manifest(secrets={"FETCH_TOKEN": "env:FETCH_TOKEN"})
    run_id = admit_mcp(store, mcp_pinned(manifest))
    adapter, sandbox_ = ToolCallingAdapter("mcp__fetch__fetch"), FakeSandbox(spawn_factory=spawner([]))
    worker = Worker(
        LocalChannel(store), lambda p: adapter, worker_id="w1", sandbox=sandbox_, credentials=Credentials(environ={})
    )
    assert worker.run_once() is True
    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason) == ("failed", "credential_missing:FETCH_TOKEN")
    assert adapter.calls == [] and sandbox_.provisioned == []


@pytest.mark.parametrize("mode", ["spawn", "initialize"])
def test_startup_failure_fails_the_run_and_tears_down_every_container(store, mode):
    fetch = mcp_manifest("fetch")
    other = mcp_manifest("other", network="none")
    run_id = admit_mcp(store, mcp_pinned(fetch, other))
    made = []

    def spawn(argv, env):
        if argv == ["mcp-server-other"] and mode == "spawn":
            raise SandboxError("spawn_failed")
        server = FakeMcpServer([tool("fetch")])
        server.silent = argv == ["mcp-server-other"]  # initialize is never answered
        made.append(server)
        return server

    adapter, sandbox_ = ToolCallingAdapter("mcp__fetch__fetch"), FakeSandbox(spawn_factory=spawn)
    assert Worker(LocalChannel(store), lambda p: adapter, worker_id="w1", sandbox=sandbox_).run_once() is True

    detail = store.get(run_id)
    assert (detail.status, detail.terminal_reason) == ("failed", "mcp_unavailable:other")
    assert adapter.calls == []
    assert len(sandbox_.sessions) == 2 and all(s.torn_down for s in sandbox_.sessions)
    assert all(s.closed for s in made)


def test_tampered_mcp_bytes_are_rejected_before_the_adapter(store):
    run_id = admit_mcp(store, mcp_pinned(mcp_manifest()))
    adapter = ToolCallingAdapter("mcp__fetch__fetch")
    sandbox_ = FakeSandbox(spawn_factory=spawner([]))
    assert Worker(TamperedMcpChannel(store), lambda p: adapter, worker_id="w1", sandbox=sandbox_).run_once() is True
    assert (store.get(run_id).status, store.get(run_id).terminal_reason) == ("failed", "definition_mismatch")
    assert adapter.calls == [] and sandbox_.provisioned == []


# --- OCI spawn command line (fake podman on PATH) ---


@pytest.fixture
def cli_bin(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    return tmp_path


def test_spawn_passes_secret_names_to_the_runtime_never_their_values(cli_bin):
    session = sandbox(cli_bin, "podman").provision(cli_profile(), CLI_RUN_ID)
    process = session.spawn(["mcp-server-fetch"], env={"FETCH_TOKEN": SECRET})
    assert process.readline(5) == b""  # the fake CLI exits; its argv is logged by then
    process.close()
    logged = calls(cli_bin, "podman")
    assert [c for c in logged if c[:1] == ["exec"] and "mcp-server-fetch" in c] == [
        ["exec", "-i", "-w", "/workspace", "-e", "FETCH_TOKEN", session.name, "mcp-server-fetch"]
    ]
    assert not any(SECRET in arg for c in logged for arg in c)


@pytest.mark.parametrize("network, expect_none_flag", [("none", True), ("open", False)])
def test_network_flag_follows_the_manifest(cli_bin, network, expect_none_flag):
    sandbox(cli_bin, "podman").provision(cli_profile(), CLI_RUN_ID, network=network)
    (run,) = [c for c in calls(cli_bin, "podman") if c[0] == "run"]
    assert ("--network=none" in run) is expect_none_flag


# --- admission ---


def admitted_worst_case(root, refs):
    agents, profiles, mcp = write_fixture(root, refs=refs)
    ledger = Ledger(root / "admit.db")
    ledger.ensure_account("default", 10**9)
    defs = Definitions(AgentRegistry(agents), ModelProfileRegistry(profiles), mcps=McpServerRegistry(mcp))
    runs = Runs(defs, ledger, execution_enabled=lambda: True, allow_dirty=lambda: True)
    record = runs.submit(RunRequest(agent_id="research-analyst", instruction="Summarise"))
    admitted = next(e for e in ledger.get(record.id).events if e.type == "task.admitted")
    return admitted.payload["worst_case_tokens"]


def test_each_mcp_server_adds_an_allowance_per_turn_to_admission(tmp_path):
    without = admitted_worst_case(tmp_path / "none", refs=())
    with_one = admitted_worst_case(tmp_path / "one", refs=("fetch@0.1.0",))
    assert with_one - without == Budget().max_turns * ADMISSION_ALLOWANCE_BYTES


def test_root_user_manifest_always_requires_rootless_runtime():
    from maef_api.mcp import sandbox_profile_for
    from maef_api.models import McpServerManifest, SandboxProfile

    base = SandboxProfile(
        schema_version="maef.dev/v1alpha1",
        id="base-profile",
        image="x@sha256:" + "a" * 64,
        cpus=1.0,
        memory_mb=128,
        pids_limit=64,
        workspace_mb=16,
        require_rootless=False,
    )
    manifest = McpServerManifest(
        schema_version="maef.dev/v1alpha1",
        id="root-server",
        version="1",
        transport="stdio",
        image="y@sha256:" + "b" * 64,
        command=["srv"],
        network="none",
        permission="read-only",
        user="0:0",
    )
    profile = sandbox_profile_for(manifest, base)
    assert (profile.user, profile.require_rootless) == ("0:0", True)
    assert sandbox_profile_for(manifest.model_copy(update={"user": None}), base).user == base.user
