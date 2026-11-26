# M1 implementation plan: `v0.1.0`

Draft, 2026-11-26. Milestone M1 of [PLAN_OF_ACTION.md](PLAN_OF_ACTION.md) (phase 0, packages 0.1–0.8).

## Aim

Anyone can clone or install MAEF, run every check with uv, and trust that a green CI means what it says. M1 ends
with a tagged `v0.1.0` release whose wheel works in an empty folder.

## Exit criteria

1. `uv sync --locked && uv run pytest` passes from a fresh clone; no `pip install` or `venv` left in docs or scripts.
2. `uv run ruff check`, `uv run ruff format --check` and `uv run ty check` are clean.
3. Plain `uv run pytest` runs unit tests only; `make test-oci test-pg test-s3` leave no containers behind.
4. The OCI suite, including the egress-proxy tests, passes in CI on Ubuntu.
5. CI is green on a pull request: lint, types, unit tests (Python 3.11–3.13, Ubuntu; macOS weekly), container suites,
   TUI tests, Studio build, dependency audit, package check.
6. In an empty folder, `uv tool install dist/maef-*.whl`, then `maef init`, `maef lint` and a run against the fake
   model all succeed; every failing command exits non-zero; `twine check` is clean.
7. LICENSE, NOTICE, TRADEMARKS.md, CHANGELOG.md and `maef --version` exist; tag `v0.1.0` has a GitHub release with
   the wheel, sdist and SBOM.

## Starting point (measured 2026-11-26)

| | Today |
|---|---|
| Environment | `pip install -e '.[dev]'` into `.venv`; no lockfile; no `[build-system]` table (setuptools by default) |
| ruff | default rules clean; 88 of 92 files would be reformatted; at line length 120: 147 long lines, 31 `raise` without `from`, 19 SQL-string, 16 subprocess findings |
| ty 0.0.111 | 100 diagnostics in `apps/api` (mostly unchecked `None`: `missing-union-attribute`, `invalid-argument-type`); about 230 including tests |
| Tests | 889 unit tests pass in 62 s; 174 container tests deselected by marker |
| Paths | `registry.py:12` `ROOT = parents[3]`, used by 9 modules (artifacts, auth, main, cli, localsetup, schemas, bench grade and report) |
| CI | none; repository is public |

## Licence

You asked for free private and business use, but no renaming. **No open-source licence can forbid renaming:** freedom
to fork is part of the definition. What you can protect is the name and the credit:

- **Apache-2.0**: free for any use, including commercial; includes a patent grant. Section 4 says redistributions
  must keep the copyright notices and the `NOTICE` file, so a renamed fork still has to say it comes from MAEF.
  Section 6 gives no right to use the project's name or logo, so a fork may not call itself MAEF or suggest you
  endorse it.
- **NOTICE**: "MAEF (Modular Agent Execution Framework), Copyright 2026 <name to show>".
- **TRADEMARKS.md**: a short policy: "MAEF" and the logo may be used to refer to the project, not as the name of a
  fork or product; modified versions must use a different name.

This is the usual pattern (Kubernetes, Rust). Not legal advice; if the name matters commercially, check it is free
and consider registering it. The alternative, a source-available licence, can restrict more but stops being open
source and puts off contributors and business users. **Recommendation: Apache-2.0 + NOTICE + TRADEMARKS.md.**

## GitHub Actions on the free plan

The repository is public, so standard GitHub-hosted runners (Linux, Windows, macOS) cost nothing. The free-plan
limits that matter are 20 concurrent jobs (5 on macOS) and 10 GB of cache per repository. If the repository ever
becomes private, it gets 2,000 minutes a month and macOS minutes count ten times, so the macOS job is weekly and
manual from the start. To stay frugal either way:

- cancel superseded runs (`concurrency` with `cancel-in-progress`); a timeout on every job;
- container suites only when the files they cover change, on `main`, and nightly;
- uv and Bun caches; actions pinned by commit SHA; `permissions: contents: read`; no secrets in CI;
- no paid model calls in CI, ever: model tests use the fake model.

Estimated per pull request: about 12 runner-minutes (lint 1, unit 3 × 2, TUI and Studio 2, container suites 5 when
they run).

## Branch and order

Work on `dev/m1-tooling`, branched from `dev/bench-findings` (which holds the roadmap). One commit or more per step;
merge to `main` when the exit criteria pass.

```
1 uv ─► 2 ruff format ─► 3 ruff rules ─► 4 test selection ─► 5 CI (ty advisory) ─► 6 ty clean ─► 5b ty blocking
                                                                   │
                                     7 egress proxy (parallel) ────┤
                                     8 package (after 4) ──────────┴─► 9 release v0.1.0
```

CI comes early so every later step is checked by it. Formatting goes first so later diffs stay readable.

## Steps

### 1. uv (0.1): Sonnet, size S

- `pyproject.toml`:
  - add `[build-system]` (setuptools for now; step 8 decides the final backend);
  - move `dev` from optional dependencies to `[dependency-groups] dev`, adding `ty`, `hypothesis` and `pip-audit`,
    with exact pins for ruff and ty so local and CI agree;
  - keep the `bedrock`, `s3` and `postgres` extras.
- `.python-version` set to 3.12; `uv lock`; commit `uv.lock`.
- Makefile: every target uses `uv run`; add `make check` (ruff, ty, unit tests).
- Docs: the README quick start becomes `uv sync` + `uv run maef init`, or `uv tool install .` for a global `maef`;
  update `RUNNING.md`, `BENCHMARKS.md` and the contributor section.
- `scripts/aws-bench/vm-setup.sh`: install uv from a pinned GitHub release tarball, verified against its published
  SHA-256 (no `curl | sh`), then `uv sync --locked --all-extras`.
- **Check:** a fresh clone in a temporary folder runs `uv sync --locked && uv run pytest`;
  `grep -rn "pip install\|python -m venv" README.md docs scripts Makefile` finds nothing.

### 2. ruff format: Haiku, size S

- `[tool.ruff]`: `line-length = 120`, `target-version = "py311"`, `extend-exclude` for generated files.
- `ruff format` over the codebase as one commit with no other changes; add its hash to `.git-blame-ignore-revs`.
- **Check:** `ruff format --check` clean; full unit suite still passes.

### 3. ruff rules: Sonnet, size S

- `select = ["E", "W", "F", "I", "B", "UP", "S", "BLE", "SIM", "RUF"]`; tests ignore `S101`, `S105`, `S106`, `S108`.
- Fix the 147 long lines, `B904` (add `from exc` or `from None` where the cause must stay hidden, such as secrets),
  `E702` and `E731`.
- Review each `S608` (SQL built from strings) and `S603`/`S607` (subprocess) finding: fix it, or add `# noqa` with a
  reason (for example, table names from a fixed list, argv lists without a shell).
- **Check:** `ruff check` clean; every `noqa` has a reason.

### 4. Test selection and fixtures (0.4): Haiku, size S

- `[tool.pytest.ini_options]`: `addopts = "-m 'not oci and not pg and not s3'"`; Makefile container targets override
  it with `-m oci` and the others.
- Postgres and SeaweedFS fixtures (`tests/conftest.py:185`, `:305`): remove the container in a `finally`, label it
  `maef-test=1`, and remove leftovers with that label at session start.
- Bring in from the readiness review:
  - the fake OpenAI-compatible model as `tests/fake_model.py` (also used by step 8);
  - the budget property test (`hypothesis`);
  - the chaos checks for R1 and R2.

  The R1 and R2 checks fail today, so they go in as `xfail(strict=True)` with the finding number. Phase 1 then has to
  flip them to passing.
- **Check:** `uv run pytest` runs unit tests only, in about a minute; `podman ps -a --filter label=maef-test=1` is
  empty after `make test-pg test-s3`, including after a failed test.

### 5. CI (0.6): Sonnet, size M

`.github/workflows/ci.yml`, on pull requests and pushes to `main`:

| Job | Runs | Notes |
|---|---|---|
| lint | `ruff check`, `ruff format --check` | |
| types | `ty check` | advisory (`continue-on-error`) until step 6, then blocking (5b) |
| unit | `uv run pytest`, Python 3.11, 3.12, 3.13 on Ubuntu | `astral-sh/setup-uv` with cache |
| tui | `bun install --frozen-lockfile && bun test` in `apps/tui` | |
| studio | `npm ci && npm run build` in `apps/studio` | no tests exist yet; the build type-checks |
| audit | `uv export --locked` piped into `pip-audit` | |
| package | step 8's wheel check | added in step 8 |

`.github/workflows/containers.yml`: OCI (rootless podman on `ubuntu-24.04`), Postgres and S3 suites. Runs on pull
requests that touch `sandbox.py`, `egress*.py`, `ledger.py`, `_sql_*.py`, `artifacts*.py`, `bench/` or
`sandbox-profiles/`; on every push to `main`; and nightly.

`.github/workflows/macos.yml`: unit tests on `macos-latest`, weekly and on manual trigger. Unit tests only: GitHub's
macOS runners can't run podman machine (no nested virtualisation).

`.github/dependabot.yml`: monthly updates for GitHub Actions and uv.

**Check:** a pull request shows every job green, apart from types, which may still be advisory; a pushed fix cancels
the superseded run.

### 6. ty clean (0.3): Sonnet, size M

- `[tool.ty]` with `python-version = "3.11"`; check `apps/api` first, then `tests`.
- Fix the 100 `apps/api` diagnostics properly: narrow `Optional` values with checks or early returns, correct wrong
  annotations, and use `typing.cast` only where an invariant guarantees the type, with a comment saying which.
- Then the tests. Rules that fire only on test doubles may be turned off for `tests/` in config, with a reason.
- Any remaining false positive of the preview release goes in `docs/` as a known issue with the ty version.
- Then 5b: make the CI types job blocking.
- **Check:** `ty check` reports 0 diagnostics with the pinned ty version; the unit suite still passes.

### 7. Egress proxy on WSL2 (0.5): Opus investigates, Sonnet fixes; timebox 1 day

- Reproduce `test_allowlisted_host_connects_through_proxy_only` on this host with podman logs and a packet capture
  inside the container network namespace.
- Hypotheses, in order:
  1. rootless networking (pasta vs slirp4netns) on WSL2 can't reach the proxy's address;
  2. the proxy listens on an address the container network doesn't route to;
  3. DNS or IPv6 resolution inside the test container;
  4. a real bug in `egress_proxy.py`.
- Run the same suite in CI on Ubuntu (step 5) to separate a host problem from a code problem.
- If the problem is specific to WSL2: document it in `RUNNING.md`, and skip the test on WSL2 with a reason (detected
  from `/proc/version`). CI stays the proof.
- **Check:** the egress tests pass in CI on Ubuntu, and pass on WSL2 or are skipped there with the documented reason.

### 8. Package (0.7): Sonnet, Haiku for the smoke test; size M

- **Package name and version:**
  - distribution name `maef`, if free on PyPI; otherwise keep `maef-control-plane` (publishing to PyPI is not part
    of M1);
  - version taken from package metadata (`importlib.metadata`), with `maef --version`.
- **Project folder instead of `parents[3]`:**
  - a new `project.py` with `project_root()`: `MAEF_HOME` if set, else the nearest parent of the current directory
    holding a `maef.toml` or an `agents/` folder;
  - the nine `ROOT` users call it lazily instead of using module-level constants;
  - `bench/report.py` takes the harness version from the package version, not `git rev-parse` of the install folder.
- **Bundled starter files:**
  - the example agents, sandbox and model-profile examples, MCP manifests, schemas and evals ship as package data
    under `maef_api/_bundled/`;
  - `maef init` in an empty folder copies them in and writes `maef.toml`;
  - in a git checkout, `init` keeps working on the repository as now.
- **Exit codes:** go through every CLI command. A failure exits 1 (2 for usage errors) with a message on stderr, and
  `maef init` stops being successful when it fails. Each case gets a test.
- **Metadata:**
  - `readme`, `license = "Apache-2.0"` and `license-files`, authors, project URLs, classifiers;
  - LICENSE, NOTICE and TRADEMARKS.md from the licence section above;
  - CHANGELOG.md in Keep a Changelog format.
- **Smoke check:** `scripts/check-wheel.sh`, also run as the CI `package` job:
  1. `uv build`, then `uvx twine check dist/*`;
  2. install the wheel with `uv tool install` into a temporary home;
  3. in an empty folder, start the fake model, `maef init` with a profile pointing at it, then `maef lint` and
     `maef run` on the starter agent;
  4. assert a succeeded run and an artifact, and that a failing command exits non-zero.
- **Check:** `check-wheel.sh` passes locally and in CI.

### 9. Release `v0.1.0` (0.8): Opus, size S

- CHANGELOG entry summarising what exists (backend steps 0–12b and 14, TUI, bench, the M1 tooling).
- `.github/workflows/release.yml` on `v*` tags: build, `twine check`, CycloneDX SBOM, then a GitHub release with
  wheel, sdist and SBOM attached. No PyPI upload in M1.
- Merge `dev/m1-tooling` to `main` and tag there.
- **Check:** the release page has the three files, and the wheel from it passes `check-wheel.sh`.

## Effort

About 6–8 working days: steps 1–4 two days; 5 one day; 6 one to two days; 7 up to one day (parallel); 8 two days;
9 half a day.

## Risks

| Risk | Response |
|---|---|
| The one-commit reformat collides with other open work | do it first; nothing else open on these files; blame-ignore file |
| ty preview changes between versions | exact pin; upgrades are their own pull requests |
| Rootless podman on GitHub's Ubuntu runner differs from WSL2 | that difference is what step 7 needs; CI is the reference |
| Bundling starter files duplicates the repo's `agents/` | build step copies them into the package; one source of truth in the repo |
| `maef` name taken on PyPI | keep `maef-control-plane` as the distribution name; the command stays `maef` |

## Decisions still needed

1. Licence: Apache-2.0 + NOTICE + TRADEMARKS.md, and the name to put in NOTICE.
2. Distribution name `maef` vs `maef-control-plane`, and whether PyPI publishing is wanted after M1.
3. OK to branch `dev/m1-tooling` from `dev/bench-findings` and merge both to `main` at the end.
