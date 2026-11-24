#!/bin/bash
# Runs as the unprivileged user. Needs: BUNDLE (path or s3:// URL of a git bundle), BASE (work dir).
set -euo pipefail
BASE=${BASE:-$HOME/maef-bench}
mkdir -p "$BASE"; cd "$BASE"
case "$BUNDLE" in s3://*) aws s3 cp --only-show-errors "$BUNDLE" maef.bundle ;; *) cp "$BUNDLE" maef.bundle ;; esac
rm -rf maef; git clone -q maef.bundle maef; cd maef; git checkout -q main
echo "revision: $(git rev-parse --short HEAD)"
"${PYTHON:-python3}" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e '.[dev]'
mkdir -p model-profiles
sed 's/^id: openrouter-inkling-2-mini/id: local-default/' examples/model-profiles/openrouter-inkling.yaml > model-profiles/local-default.yaml
.venv/bin/maef lint
podman info --format '{{.Host.Security.Rootless}} {{.Version.Version}} {{.Host.CgroupsVersion}}'
podman pull -q "$(awk '/^image:/ {print $2}' sandbox-profiles/python-sandbox.yaml)" >/dev/null
# Preflight: the real container tests (sandbox + bench grader).
.venv/bin/pytest -q -m oci tests/test_sandbox_oci.py tests/test_bench_grade.py 2>&1 | tail -3
echo SETUP_OK
