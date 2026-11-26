"""Streaming output cap (R4): a command cannot exhaust worker memory by printing without end."""

import json
import resource
import sys
import time

from maef_api.models import SandboxProfile
from maef_api.sandbox import ExecResult, FakeSandbox, run_capped
from maef_api.tools import BuiltinTools

PROFILE = SandboxProfile(
    schema_version="maef.dev/v1alpha1",
    id="restricted-local",
    image="docker.io/library/busybox:1.37",
    cpus=1,
    memory_mb=64,
    pids_limit=32,
    workspace_mb=8,
)


def _rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def test_gigabyte_of_output_is_capped_quickly_with_bounded_memory():
    script = "import sys\nb=b'y'*65536\nwhile True: sys.stdout.buffer.write(b)"  # unbounded, ~GB/s
    before = _rss_kb()
    start = time.monotonic()
    res = run_capped([sys.executable, "-c", script], stdin=None, timeout=60, max_bytes=16 * 1024)
    elapsed = time.monotonic() - start
    growth_mb = (_rss_kb() - before) / 1024
    print(f"elapsed={elapsed:.2f}s rss_growth={growth_mb:.1f}MB discarded={res.discarded_bytes}")
    assert elapsed < 10
    assert res.truncated and not res.timed_out
    assert len(res.stdout) == 16 * 1024 and res.discarded_bytes > 0
    assert growth_mb < 50


def test_timeout_returns_capped_prefix():
    script = "import sys,time\nsys.stdout.write('hello');sys.stdout.flush()\ntime.sleep(30)"
    res = run_capped([sys.executable, "-c", script], stdin=None, timeout=1, max_bytes=1024)
    assert res.timed_out and not res.truncated and res.stdout == b"hello"


def test_small_output_untouched_and_stdin_delivered():
    res = run_capped(["cat"], stdin=b"abc", timeout=10, max_bytes=1024)
    assert (res.exit_code, res.stdout, res.truncated, res.discarded_bytes) == (0, b"abc", False, 0)


def test_run_command_reports_truncation_to_the_model():
    session = FakeSandbox(lambda argv, stdin: ExecResult(0, b"x" * 100_000, b"")).provision(PROFILE, "r1")
    result = BuiltinTools(["run_command"], {}, session=session).invoke("run_command", {"argv": ["ls"]})
    body = json.loads(result.content)
    assert body["truncated"] is True and "output truncated at" in body["note"] and "command stopped" in body["note"]
