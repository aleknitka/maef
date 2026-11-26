import io
import json
import sys

from maef_api.cli import main
from maef_api.client import MaefClient


def cli(live_api, *argv):
    return main(list(argv), client=MaefClient(live_api.transport))


def submit_and_finish(live_api):
    run_id = MaefClient(live_api.transport).submit("research-analyst", "Hello")["run_id"]
    live_api.finish_queued()
    return run_id


def test_read_commands_print_expected_text(live_api, capsys):
    run_id = submit_and_finish(live_api)
    assert cli(live_api, "agents") == 0
    assert "research-analyst" in capsys.readouterr().out
    assert cli(live_api, "status", run_id) == 0
    assert capsys.readouterr().out.startswith(f"{run_id} succeeded")
    assert cli(live_api, "artifacts", run_id) == 0
    assert "output.md" in capsys.readouterr().out
    assert cli(live_api, "result", run_id) == 0
    assert capsys.readouterr().out == "Final answer.\n"


def test_json_output_parses(live_api, capsys):
    run_id = submit_and_finish(live_api)
    for argv in (["agents"], ["status", run_id], ["artifacts", run_id], ["result", run_id]):
        assert cli(live_api, *argv, "--json") == 0
        json.loads(capsys.readouterr().out)


def test_run_wait_prints_events_to_stderr_and_result_to_stdout(live_api, capsys):
    live_api.finish_later(0.1)
    assert cli(live_api, "run", "research-analyst", "Hello", "--wait", "--timeout", "5") == 0
    captured = capsys.readouterr()
    assert captured.out == "Final answer.\n"
    assert captured.err.startswith("run ") and "task.created" in captured.err and "task.succeeded" in captured.err


def test_failed_run_exits_1_without_leaking_error_text(live_api, capsys):
    live_api.adapter.error = RuntimeError("model down")
    live_api.finish_later(0.1)
    assert cli(live_api, "run", "research-analyst", "Hello", "--wait", "--timeout", "5") == 1
    err = capsys.readouterr().err
    assert "provider_error" in err and "model down" not in err


def test_wait_timeout_exits_3(live_api, capsys):
    assert cli(live_api, "run", "research-analyst", "Hello", "--wait", "--timeout", "0.3") == 3
    assert "timed out" in capsys.readouterr().err


def test_unknown_agent_exits_1(live_api, capsys):
    assert cli(live_api, "run", "no-such-agent", "Hello", "--wait") == 1
    assert "Agent not found" in capsys.readouterr().err


def test_instruction_dash_reads_stdin(live_api, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("Summarise the attached notes\n"))
    live_api.finish_later(0.1)
    assert cli(live_api, "run", "research-analyst", "-", "--wait", "--timeout", "5") == 0
    assert "Summarise the attached notes" in repr(live_api.adapter.calls)


def test_get_writes_bytes_and_refuses_overwrite_without_force(live_api, tmp_path):
    run_id = submit_and_finish(live_api)
    digest = MaefClient(live_api.transport).artifacts(run_id)[0]["digest"]
    dest = tmp_path / "out.md"
    assert cli(live_api, "get", digest, "-o", str(dest)) == 0
    assert dest.read_bytes() == b"Final answer."
    dest.write_bytes(b"keep me")
    assert cli(live_api, "get", digest, "-o", str(dest)) == 1
    assert dest.read_bytes() == b"keep me"
    assert cli(live_api, "get", digest, "-o", str(dest), "--force") == 0
    assert dest.read_bytes() == b"Final answer."
