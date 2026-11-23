"""The python-smoke test agent: its definition lints clean, its eval suite and sandbox profile parse, and a run
that writes and executes a script works end to end in a real container.

The end-to-end test is a harness-level test: `execute()` with a real `LedgerSink`, a real `OciCliSandbox`
session for the `python-sandbox` profile, `collect_outputs` for artifacts, and a scripted model. Nothing
is written into the repository: artifacts and the ledger live under tmp_path.
"""
import json
import re
import subprocess

import pytest
import yaml
from maef_api.adapters.base import ModelResponse, ToolCall
from maef_api.artifacts import LocalArtifactStore
from maef_api.definitions import PinnedDefinition
from maef_api.evals import load_suite
from maef_api.harness import LedgerSink, PreparedRun, execute
from maef_api.ledger import Ledger
from maef_api.lint import lint_repo
from maef_api.models import AgentDefinition, Budget
from maef_api.outputs import collect_outputs
from maef_api.registry import ROOT, SandboxProfileRegistry
from maef_api.sandbox import detect_sandbox
from maef_api.tools import BuiltinTools
from test_run_execution import profile

AGENT_DIR = ROOT / "agents" / "python-smoke"
SCRIPT = "for i in range(1, 6):\n    print(i * i)\n"
EXPECTED_STDOUT = "1\n4\n9\n16\n25\n"
SAVE_ARGV = ["sh", "-c", "mkdir -p /workspace/out && cat > /workspace/out/script.py"]
RUN_ARGV = ["python3", "/workspace/out/script.py"]
GOOD_OUTPUT = "**Script**\n```\n" + SCRIPT + "```\n\n**Output**\n```\n" + EXPECTED_STDOUT + "```\n"
SANDBOX = detect_sandbox()


# --- A. non-container tests (always run) ---

def test_lint_reports_no_errors_for_python_smoke():
    findings = lint_repo(ROOT)
    mine = [f for f in findings
            if f.path.startswith("agents/python-smoke/") or f.path == "sandbox-profiles/python-sandbox.yaml"]
    assert [str(f) for f in mine if f.severity == "error"] == []


def test_python_smoke_eval_suite_loads_and_regex_matches_output():
    suite = load_suite(ROOT / "evals" / "python-smoke.yaml")
    assert suite.id == "python-smoke"
    patterns = [c.regex for t in suite.tasks for c in t.checks if c.regex is not None]
    assert patterns, "suite has no regex check"
    for pattern in patterns:
        assert re.search(pattern, GOOD_OUTPUT) is not None
        assert re.search(pattern, "1 4 9 16 24") is None


def test_python_sandbox_profile_is_locked_down():
    sandbox = SandboxProfileRegistry().get_profile("python-sandbox")
    assert sandbox is not None
    assert sandbox.network == "none"
    assert sandbox.require_rootless is True
    assert sandbox.user == "65534:65534"
    assert "@sha256:" in sandbox.image


# --- B. real container, end to end (needs podman or docker) ---

class ScriptedModel:
    """Turn 1 saves the script, turn 2 runs it, turn 3 answers. Records every request it receives."""

    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        turn = len(self.calls)
        if turn == 1:
            return ModelResponse("", (ToolCall("t1", "run_command", {"argv": SAVE_ARGV, "stdin": SCRIPT}),),
                                 "tool_use", 100, 10)
        if turn == 2:
            return ModelResponse("", (ToolCall("t2", "run_command", {"argv": RUN_ARGV}),), "tool_use", 100, 10)
        return ModelResponse(GOOD_OUTPUT, (), "end_turn", 100, 10)


def _labelled_containers(run_id):
    out = subprocess.run([SANDBOX.runtime, "ps", "-a", "-q", "--filter", f"label=maef.run={run_id}"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.oci
@pytest.mark.skipif(SANDBOX is None, reason="no podman or docker runtime found")
def test_python_smoke_writes_runs_and_collects_script(tmp_path):
    agent_yaml = yaml.safe_load((AGENT_DIR / "agent.yaml").read_text(encoding="utf-8"))
    agent = AgentDefinition.model_validate(agent_yaml)
    budget = Budget.model_validate(agent_yaml["budget"])
    instruction = "Write a Python script that prints the squares of the whole numbers 1 to 5, one per line, then run it."
    pinned = PinnedDefinition(agent=agent, profile=profile(), instructions=(AGENT_DIR / "SYSTEM.md").read_text(encoding="utf-8"),
                              git_rev="test", dirty=True, definition_digest="d", profile_digest="p")

    store = Ledger(tmp_path / "smoke.db")
    store.ensure_account("default", 5_000_000)
    # The agent directory is not committed yet, so admission needs allow_dirty (as the API tests do).
    run_id = store.admit(pinned, budget, prompt_bytes=len(instruction.encode()) + 2000, instruction=instruction,
                         allow_dirty=True)
    reserved = store.get(run_id).reserved_tokens

    sandbox_profile = SandboxProfileRegistry().get_profile(agent.runtime.sandbox_profile)
    if not SANDBOX.info()["rootless"]:
        sandbox_profile = sandbox_profile.model_copy(update={"require_rootless": False})  # CI runtime is rootful
    session = SANDBOX.provision(sandbox_profile, run_id)
    artifacts = LocalArtifactStore(tmp_path / "blobs")
    model = ScriptedModel()
    try:
        run = PreparedRun(run_id, pinned, budget, reserved, instruction,
                          tools=BuiltinTools(["run_command"], {}, session=session),
                          outputs=lambda: collect_outputs(session))
        execute(LedgerSink(store, run_id, artifacts=artifacts), run, model)
    finally:
        session.teardown()

    detail = store.get(run_id)
    assert detail.status == "succeeded", detail.terminal_reason

    # Turn 2's tool result, as the model received it on turn 3.
    assert len(model.calls) == 3
    results = {m.tool_call_id: m for m in model.calls[2]["messages"] if m.role == "tool"}
    assert results["t1"].is_error is False and results["t2"].is_error is False
    run_result = json.loads(results["t2"].text)
    assert run_result["exit_code"] == 0
    assert run_result["stdout"] == EXPECTED_STDOUT
    assert [e.payload["is_error"] for e in detail.events if e.type == "tool.result"] == [False, False]

    # The script the model wrote is stored as an artifact, byte for byte.
    manifest = next(e.payload for e in detail.events if e.type == "run.manifest")
    stored = {a["name"]: a for a in manifest["artifacts"]}
    assert "script.py" in stored
    assert artifacts.read(stored["script.py"]["digest"]) == SCRIPT.encode()

    # Teardown removed the run's container.
    assert _labelled_containers(run_id) == ""
