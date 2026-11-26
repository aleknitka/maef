#!/bin/bash
# Runs as the unprivileged user. Needs: BUNDLE (path or s3:// URL of a git bundle), BASE (work dir).
set -euo pipefail
BASE=${BASE:-$HOME/maef-bench}
mkdir -p "$BASE"; cd "$BASE"
case "$BUNDLE" in s3://*) aws s3 cp --only-show-errors "$BUNDLE" maef.bundle ;; *) cp "$BUNDLE" maef.bundle ;; esac
rm -rf maef; git clone -q maef.bundle maef; cd maef; git checkout -q main
echo "revision: $(git rev-parse --short HEAD)"
# uv from the pinned release tarball, verified against its published SHA-256 (no curl | sh).
UV_VERSION=0.13.9
UV_SHA256=a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5
mkdir -p "$HOME/.local/bin"
curl -fsSL -o uv.tar.gz "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
echo "$UV_SHA256  uv.tar.gz" | sha256sum -c -
tar -xzf uv.tar.gz --strip-components=1 -C "$HOME/.local/bin" uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx
rm -f uv.tar.gz
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv sync --locked --all-extras
mkdir -p model-profiles
sed 's/^id: openrouter-inkling-2-mini/id: local-default/' examples/model-profiles/openrouter-inkling.yaml > model-profiles/local-default.yaml
uv run maef lint
podman info --format '{{.Host.Security.Rootless}} {{.Version.Version}} {{.Host.CgroupsVersion}}'
podman pull -q "$(awk '/^image:/ {print $2}' sandbox-profiles/python-sandbox.yaml)" >/dev/null
# Preflight: the real container tests (sandbox + bench grader).
uv run pytest -q -m oci tests/test_sandbox_oci.py tests/test_bench_grade.py 2>&1 | tail -3
echo SETUP_OK
