# Use cases

What MAEF is for, with an example of each. MAEF has two outputs: **experiments** on AI agents in a controlled,
isolated environment, and **specialist agents**, self-contained modules that come out of those experiments and can be
used off the shelf. Most use cases below are one or the other, or the path from the first to the second.

Status is shown honestly for each one: **now** (works in v0.1.0), **partly** (the pieces exist, the workflow
doesn't yet) or **planned** (with the [roadmap](ROADMAP.md) phase).

| # | Use case | For | Status |
|---|---|---|---|
| 1 | [Controlled testing of AI compositions](#1-controlled-testing-of-ai-compositions) | researchers, agent builders | partly; full in phases 2–4 |
| 2 | [Off-the-shelf specialist agents](#2-off-the-shelf-specialist-agents) | developers, teams | now; releases in phase 6 |
| 3 | [Choosing a model on evidence](#3-choosing-a-model-on-evidence) | teams buying model access | partly; phase 3–4 |
| 4 | [Watching for model and provider drift](#4-watching-for-model-and-provider-drift) | anyone depending on a hosted model | planned, phases 2–3 |
| 5 | [Evaluating tools and MCP servers safely](#5-evaluating-tools-and-mcp-servers-safely) | agent builders, security reviewers | partly |
| 6 | [Prompt and skill engineering with evidence](#6-prompt-and-skill-engineering-with-evidence) | agent builders | partly; phases 2–4 |
| 7 | [Safety and containment testing](#7-safety-and-containment-testing) | security and red teams | partly |
| 8 | [Studying how agents work](#8-studying-how-agents-work) | researchers | partly |
| 9 | [Evaluation that memorisation can't game](#9-evaluation-that-memorisation-cant-game) | researchers, benchmark authors | now (one task family) |
| 10 | [Reproducible, publishable results](#10-reproducible-publishable-results) | researchers, reviewers | planned, phase 5 |
| 11 | [Comparing agent frameworks](#11-comparing-agent-frameworks) | researchers, platform teams | planned, phase 3 |
| 12 | [Bounded delegation from coding assistants](#12-bounded-delegation-from-coding-assistants) | developers using Claude Code or Codex | now |
| 13 | [Private and offline agent work](#13-private-and-offline-agent-work) | regulated or air-gapped settings | now |
| 14 | [Regression gates for agents in CI](#14-regression-gates-for-agents-in-ci) | teams shipping agents | planned, phase 6 |

## 1. Controlled testing of AI compositions

An agent is a composition: a model, its settings (reasoning effort, temperature, output limit), a system prompt,
skills, tools and a budget, run by a harness. MAEF changes **one part at a time**, keeps the rest fixed, and runs
each version in the same sealed sandbox on the same seeded tasks, graded by hidden cases.

- *Example:* does putting the TDD skill in the prompt beat letting the agent read it on demand? Two arms, same model,
  same 5 seeds × 3 repeats; compare hidden pass rate, test-first verdict, tokens and cost.
- *Ablation:* remove one skill or one tool and measure what it was worth.
- *Sensitivity:* `reasoning_effort` low, medium, high on the same tasks; where does extra thinking stop paying off?
- *Why isolation matters:* no network, read-only root, capped memory and processes, pinned images and pinned agent
  files, so a difference between arms comes from the change, not the environment.
- *Today:* `maef bench` runs one agent on seeded, noisy tasks with hidden grading and records everything. Arms,
  experiment specs, paired statistics and full condition manifests come in phases 2–4.

## 2. Off-the-shelf specialist agents

A MAEF agent is a folder: instructions, skills, tool grants, model and sandbox profiles, budget. It is pinned by git
revision and file digest, so it runs the same wherever MAEF is installed. Once an agent is good at a job, it is a
module others can use without knowing how it is built.

- *Example:* a `research-analyst` that reads web pages through an allowlisted fetch tool and returns a sourced
  summary; a `tdd-engineer` that writes tested code in a Python sandbox.
- *Ways to use one:* `maef run <agent> "<task>"` from a terminal or script; from Claude Code or Codex through the
  `maef mcp` gateway; through the HTTP API from your own code.
- *What the user gets:* a bounded run (token, time and turn limits), the result as a downloadable file, and a full
  record of what the agent did.
- *Today:* all of the above works. Planned (phase 6): `maef agent promote` to release the winning version of an
  experiment, with a link to the evidence, and specialist bundles to install on another machine.

## 3. Choosing a model on evidence

Run the same agent and tasks against several models or providers and compare quality, cost, latency and reliability,
instead of choosing from leaderboards that measure something else.

- *Example:* a small open model through OpenRouter against a frontier model for the `tdd-engineer`: does the cheaper
  model reach the same hidden pass rate, and at what cost per solved task?
- *Today:* run `maef bench` once per model profile and compare reports (cost and tokens are recorded per run). One
  experiment with arms and intervals comes in phases 3–4.

## 4. Watching for model and provider drift

Hosted models change without notice, and routers such as OpenRouter may serve the same model name from different
providers. Re-running a fixed experiment on a schedule shows when results move.

- *Example:* a weekly rerun of last month's winning configuration; an alert when the pass rate leaves its interval or
  the served provider changes.
- *Planned:* recording the served provider and model version per turn (phase 2), provider pinning (phase 2) and
  scheduled experiments (phase 3).

## 5. Evaluating tools and MCP servers safely

Third-party tools and MCP servers can be tried inside the sandbox before anyone trusts them: the server runs in its
own container, reaches only allowlisted hosts through the egress proxy, and every call is traced.

- *Example:* does adding a web-search MCP server improve a research agent enough to justify its cost and risk? Arm A
  without it, arm B with it.
- *Today:* pinned MCP servers in containers, the egress allowlist and tool traces work. Comparing with and without a
  tool as an experiment comes with arms (phases 2–3).

## 6. Prompt and skill engineering with evidence

Treat a system prompt or a skill like code: change it on a branch, run the experiment, keep the change only if the
numbers say so.

- *Example:* the 2026-11-24 runs showed one model spending a whole turn reasoning without calling a tool; the fix ("every
  turn should end in a tool call") was confirmed by rerunning the same seed. With experiments, that becomes a two-arm
  comparison over many seeds instead of one rerun.
- *Today:* agents live in git, drafts and diffs work, every run records the exact pinned files. `maef agent render`
  (see exactly what the model receives) and variants come in phase 2.

## 7. Safety and containment testing

Give an agent a task in a sealed environment and record what it tries to do: commands, network attempts, files
written, budget hit. Feed it hostile input (a prompt-injected web page, a misleading task) and see what happens.

- *Example:* does a research agent follow instructions hidden in a fetched page? Count blocked network attempts and
  check whether the output leaks the injection.
- *Today:* sandbox, egress allowlist, budget guard and full transcripts work. Safety metrics as part of experiment
  reports come in phase 4; the open containment gaps (R3, R4 in the readiness review) are fixed in phase 1.

## 8. Studying how agents work

The transcript records every prompt, reply, reasoning text, tool call and result, so the process can be measured,
not just the outcome.

- *Example:* the TDD trace classifies whether an agent wrote tests before code and how big its red-to-green cycles
  were; the same approach can measure planning, retries, or how agents recover from errors.
- *Today:* transcripts and the TDD trace work. More process metrics, versioned and re-scorable, come in phase 4;
  `maef bench replay` (a turn-by-turn view) in phase 2.

## 9. Evaluation that memorisation can't game

Benchmarks leak into training data. MAEF's task families generate seeded variants with changed rules (pins, frames,
bonus rules, names), so an agent that recalls the familiar version fails the hidden cases.

- *Example:* the bowling family at noise 2: in the 2026-11-24 runs the one failure was an agent applying the
  familiar reset rule instead of the changed one the task described.
- *Today:* one family (bowling) with three noise levels and 45 hidden cases. A second family comes in phase 4.

## 10. Reproducible, publishable results

An experiment should be something others can find, inspect and rerun: the spec, every run's conditions, the raw
records, the metrics and the report, in open formats.

- *Example:* a paper's results shipped as an exported bundle; a reviewer reruns it on a fresh cloud machine with one
  script and gets the same conclusion within the reported intervals.
- *Planned:* FAIR export with RO-Crate and provenance, and `maef experiment rerun` (phase 5). Today every run is
  recorded and the AWS benchmark is recreatable from one script (`scripts/aws-bench/run.sh`).

## 11. Comparing agent frameworks

The harness is a factor too. An outside agent framework can run as an arm inside the same sandbox, reach the model
through a MAEF metering proxy, and be graded by the same hidden cases, so frameworks are compared on equal terms.

- *First frameworks:* LangChain **Deep Agents** (planning, virtual filesystem, subagents, skills) and **AWS Strands
  Agents** (model-driven loop, MCP tools, multi-agent patterns).
- *Example:* `tdd-engineer` under MAEF's loop, Deep Agents and Strands with the same model and seeds: pass rate,
  test-first verdict, tokens and cost, with subagent calls counted against the same budget.
- *Also:* an agent already written in Deep Agents or Strands runs contained and budgeted, as an off-the-shelf
  specialist (use case 2).
- *Planned:* the model proxy, the harness seam and both adapters ([HARNESSES.md](HARNESSES.md), package 3.6).

## 12. Bounded delegation from coding assistants

A general coding assistant hands a specialist task to a MAEF agent instead of loading that specialist's context,
tools and MCP servers itself. The parent gets the result, a token count and a trace; the specialist runs within its
own budget.

- *Example:* Claude Code asks `research-analyst` to check a library's licence and changelog; the fetch tool and its
  web pages never enter Claude Code's context.
- *Today:* the `maef mcp` gateway works with Claude Code and Codex. Output escaping (R3) is fixed in phase 1.

## 13. Private and offline agent work

Everything can run on one machine: the database is a file, results go to a local folder, models can be served by
Ollama or LM Studio, and containers run in Podman. Nothing needs a cloud account.

- *Example:* evaluating agents on confidential code or documents with a local model, in a setting where data must not
  leave the machine.
- *Today:* works; cloud services (OpenRouter, Bedrock, S3, Postgres) are optional.

## 14. Regression gates for agents in CI

A team that ships an agent keeps its experiment as a test: a new prompt, skill or model version must not do worse
than the released one before it can be released.

- *Example:* a pull request that edits an agent's `SYSTEM.md` triggers a small experiment; the merge is blocked if the
  pass rate drops beyond its interval.
- *Planned:* the regression gate (phase 6, package 6.3). Paid model calls in CI stay opt-in.
