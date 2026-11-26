#!/bin/bash
# Runs as the unprivileged user after vm-setup.sh. Needs OPENROUTER_API_KEY (or KEY_PARAM to read it from SSM).
# Runs the given seeds, then (if RESULTS is an s3:// URL) uploads a tarball of everything.
set -uo pipefail
BASE=${BASE:-$HOME/maef-bench}; cd "$BASE/maef"
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  OPENROUTER_API_KEY=$(aws ssm get-parameter --name "$KEY_PARAM" --with-decryption --query Parameter.Value --output text)
fi
export OPENROUTER_API_KEY
D="$BASE/data"; rm -rf "$D"; mkdir -p "$D"
export MAEF_DB="$D/maef.db" MAEF_ARTIFACTS_DIR="$D/artifacts"
export PATH="$HOME/.local/bin:$PATH"
M="uv run --locked maef"; PY="uv run --locked python"; PORT=${PORT:-8767}
export MAEF_API_TOKEN=$($M admin token create --name bench --scopes read,runs:write 2>/dev/null)
export MAEF_WORKER_CREDENTIAL=$($M admin worker create --id "bench-$(hostname)" 2>/dev/null)
MAEF_EXECUTION_ENABLED=1 $PY -m uvicorn maef_api.main:app --app-dir apps/api --host 127.0.0.1 --port $PORT > "$D/api.log" 2>&1 &
API=$!
for i in $(seq 30); do curl -sf http://127.0.0.1:$PORT/health >/dev/null 2>&1 && break; sleep 1; done
curl -sf http://127.0.0.1:$PORT/health >/dev/null || { echo "API did not start"; tail -20 "$D/api.log"; exit 1; }
MAEF_RECORD_TRANSCRIPT=1 MAEF_API_URL=http://127.0.0.1:$PORT $PY -m maef_api.worker > "$D/worker.log" 2>&1 &
WK=$!
{ echo "host: $(hostname) $(uname -sr) $(nproc) cpu $(free -m | awk '/Mem:/ {print $2}') MB";
  echo "revision: $(git rev-parse HEAD)"; podman --version; $PY --version; date -u +%FT%TZ; } > "$D/environment.txt"
for spec in ${RUNS:-"7:2"}; do
  seed=${spec%%:*}; noise=${spec##*:}
  echo "== bench seed=$seed noise=$noise"
  MAEF_API_URL=http://127.0.0.1:$PORT $M bench run --family bowling --seed "$seed" --noise "$noise" \
     --agent tdd-engineer --out "$D/bench-results" --timeout 1800 > "$D/bench-$seed-$noise.log" 2>&1
  echo "exit=$?"; grep -m3 -E "Status:|Verdict|passed\)" "$D/bench-$seed-$noise.log" || true
done
MAEF_API_URL=http://127.0.0.1:$PORT curl -sf -H "Authorization: Bearer $MAEF_API_TOKEN" http://127.0.0.1:$PORT/api/v1/metrics > "$D/metrics.txt" 2>/dev/null || true
kill -INT $WK; sleep 3; kill $API $WK 2>/dev/null; wait 2>/dev/null
cp "$MAEF_DB" "$D/ledger-copy.db" 2>/dev/null || true
if [[ "${RESULTS:-}" == s3://* ]]; then
  tar -C "$BASE" -czf "$BASE/results.tgz" data && aws s3 cp --only-show-errors "$BASE/results.tgz" "$RESULTS" && echo "uploaded $RESULTS"
fi
echo BENCH_DONE
