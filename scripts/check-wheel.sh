#!/usr/bin/env bash
# Build the wheel and sdist, then prove the wheel works on its own: install it into a throwaway tool directory,
# set up a project in an empty folder, and do one run against the fake model (no paid model, no container runtime).
# Usage: bash scripts/check-wheel.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PIDS=()

cleanup() {
  local pid
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$WORK"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

free_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

# Wait until $1 (a command string) succeeds, for up to 30 seconds; show the log $2 when it does not.
wait_for() {
  local i
  for i in $(seq 1 60); do
    if eval "$1" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  [ -z "${2:-}" ] || sed 's/^/  | /' "$2" >&2 || true
  fail "timed out waiting for: $1"
}

# Nothing from the caller's environment may leak into the check (tokens, database or repository overrides).
for var in $(env | sed -n 's/^\(MAEF_[A-Z0-9_]*\)=.*/\1/p'); do unset "$var"; done
unset UV_LOCKED UV_FROZEN

export UV_TOOL_DIR="$WORK/tools"
export UV_TOOL_BIN_DIR="$WORK/bin"
export PATH="$WORK/bin:$PATH"

step "build the wheel and sdist"
(cd "$REPO" && uv build --out-dir "$WORK/dist")
ls "$WORK/dist"
uvx twine check "$WORK"/dist/*

step "install the wheel as a tool (temporary tool directories)"
WHEEL="$(ls "$WORK"/dist/*.whl)"
uv tool install "$WHEEL"

step "start the fake model"
MODEL_PORT="$(free_port)"
python3 "$REPO/tests/fake_model.py" --port "$MODEL_PORT" >"$WORK/model.log" 2>&1 &
PIDS+=("$!")
wait_for "(exec 3<>/dev/tcp/127.0.0.1/$MODEL_PORT)" "$WORK/model.log"

step "maef --version, init and lint in an empty folder"
PROJECT="$WORK/project"
mkdir "$PROJECT"
cd "$PROJECT"
VERSION_LINE="$(maef --version)"
echo "$VERSION_LINE"
case "$VERSION_LINE" in "maef "[0-9]*) ;; *) fail "unexpected --version output: $VERSION_LINE" ;; esac
maef init --model-id fake --base-url "http://127.0.0.1:$MODEL_PORT/v1"
[ -f maef.toml ] || fail "init did not write maef.toml"
[ -f agents/research-analyst/agent.yaml ] || fail "init did not copy the starter agents"
maef lint

step "add an agent that needs only model calls (every starter agent needs a container runtime)"
mkdir agents/wheel-smoke
cat >agents/wheel-smoke/agent.yaml <<'YAML'
schema_version: maef.dev/v1alpha1
id: wheel-smoke
version: 0.1.0
display_name: Wheel Smoke
description: Asks the model for the time once and reports it. Needs no container runtime.
capabilities: [smoke-test]
instructions: SYSTEM.md
skills: []
tools: [current_time]
mcp_servers: []
permissions:
  network_open: false
runtime:
  model_profile: local-default
  sandbox_profile: restricted-local
budget:
  max_tokens: 20000
  max_duration_seconds: 120
  max_turns: 4
outputs: [report-markdown]
YAML
printf 'Use the current_time tool once, then answer in one sentence.\n' >agents/wheel-smoke/SYSTEM.md
git add agents/wheel-smoke
git -c user.name=check-wheel -c user.email=check-wheel@localhost -c commit.gpgsign=false commit --quiet -m "Add wheel-smoke agent"
maef lint

step "start the API and the worker"
API_PORT="$(free_port)"
export MAEF_API_URL="http://127.0.0.1:$API_PORT"
MAEF_API_TOKEN="$(maef admin token create --name wheel-check --scopes read,runs:write 2>/dev/null)"
export MAEF_API_TOKEN
MAEF_WORKER_CREDENTIAL="$(maef admin worker create --id wheel-worker 2>/dev/null)"
export MAEF_WORKER_CREDENTIAL
MAEF_EXECUTION_ENABLED=1 maef api --port "$API_PORT" >"$WORK/api.log" 2>&1 &
PIDS+=("$!")
wait_for "maef agents" "$WORK/api.log"
maef worker >"$WORK/worker.log" 2>&1 &
PIDS+=("$!")

step "run the smoke agent"
RESULT="$(maef run wheel-smoke "What time is it?" --wait --timeout 90 --json)" || {
  sed 's/^/  api    | /' "$WORK/api.log" >&2
  sed 's/^/  worker | /' "$WORK/worker.log" >&2
  fail "maef run exited non-zero"
}
echo "$RESULT"
RUN_ID="$(printf '%s' "$RESULT" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d["status"] == "succeeded", d; print(d["run_id"])')"
maef artifacts "$RUN_ID" | tee "$WORK/artifacts.txt"
grep -q 'output.md' "$WORK/artifacts.txt" || fail "run $RUN_ID produced no output.md artifact"

step "a failing command exits non-zero"
set +e
maef run no-such-agent "hello" >/dev/null 2>"$WORK/fail.err"
CODE=$?
set -e
cat "$WORK/fail.err"
[ "$CODE" -eq 1 ] || fail "expected exit code 1 for an unknown agent, got $CODE"

printf '\ncheck-wheel: OK\n'
