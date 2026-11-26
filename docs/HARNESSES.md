# Harnesses: Deep Agents and AWS Strands

Draft, 2026-11-26. How MAEF runs agents built with other frameworks, starting with
[LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) (`deepagents`, MIT, 0.7.41) and
[AWS Strands Agents](https://strandsagents.com/) (`strands-agents`, Apache-2.0, 1.65.0). This expands package 3.6
of [PLAN_OF_ACTION.md](PLAN_OF_ACTION.md).

## Why

The harness, meaning the loop that calls the model and runs tools, is a factor in an agent's results, just like the
model or the prompt. Deep Agents adds planning (`write_todos`), a virtual filesystem, subagents (`task`) and skills on
top of LangGraph. Strands is a model-driven loop with native Bedrock support, MCP tools and multi-agent patterns
(swarm, graph). MAEF should be able to:

1. **compare them fairly** with MAEF's own loop: same model, tasks, sandbox, budget and hidden grading
   ([use case 11](USE_CASES.md#11-comparing-agent-frameworks));
2. **run agents already written** in either framework, contained and budgeted, as off-the-shelf specialists
   ([use case 2](USE_CASES.md#2-off-the-shelf-specialist-agents)).

## Design: the harness runs inside the sandbox; the model is reached only through MAEF

```
worker ── provision ──► podman, per run, internal network only
   │                      ├─ harness container  (pinned image: deepagents or strands + MAEF entry point)
   │                      │    reads /maef/run.json, works in /workspace, writes /workspace/out/
   │                      │    OPENAI_BASE_URL=http://model-proxy:8080/v1   (no other route out)
   │                      └─ MCP server containers (as today, behind the egress allowlist)
   │
   └── model proxy (in the worker) ── MAEF ModelAdapter ──► OpenRouter / Bedrock / Anthropic / Ollama ...
         OpenAI-compatible endpoint; run-scoped key; counts tokens and cost; enforces the budget;
         records every request and response as the transcript; refuses calls once the budget is spent
```

Why this shape:

- **Equal footing.** Every harness reaches the model through the same proxy, so tokens, cost, latency, budget limits
  and transcripts are measured the same way for all of them. Subagent calls (Deep Agents `task`, Strands swarm) go
  through the proxy too, so they are counted and can't escape the budget.
- **Real containment.** The framework's own code and tools run in the sandbox. Deep Agents' `LocalShellBackend` and
  Strands' shell and file tools have no isolation of their own, and here they don't need it: the container provides
  it (no network, read-only root, capped memory and processes, user nobody).
- **Any model profile.** The proxy is a thin OpenAI-compatible front over MAEF's existing adapters, so a Bedrock,
  Anthropic or Ollama profile works for every harness, not only what each framework supports.
- **Dependency isolation.** LangChain/LangGraph and Strands dependencies live in their images, locked and pinned by
  digest, not in MAEF's own environment.
- **The same process metrics.** Tool calls appear in model responses and tool results in the next request, so the
  transcript from the proxy feeds the TDD trace and the other metrics for any harness.

The alternative, importing the frameworks into the worker and routing their tools to MAEF's sandbox, needs less code
at first. But the framework then runs on the host, its dependencies collide with MAEF's, and its measurements depend
on hooks each framework exposes differently. Not recommended.

## How an agent definition maps

`agent.yaml` gets `runtime.harness`:

```yaml
runtime:
  harness: deepagents        # maef (default) | deepagents | strands
  harness_options:           # passed to the adapter, recorded in the manifest
    subagents: true          # deepagents: keep the general-purpose subagent
    planning: true           # deepagents: write_todos
```

| MAEF definition | MAEF loop | Deep Agents | Strands |
|---|---|---|---|
| `instructions` (SYSTEM.md) | system prompt | `system_prompt` | `system_prompt` |
| skills | `read_agent_file` | native `skills` (SKILL.md folders, read through its filesystem) | a read-only file tool over the skills folder (or inline, with `skills_mode`) |
| `run_command` | sandbox exec | `execute` via a shell backend rooted at `/workspace` | a `shell` tool restricted to `/workspace` |
| `read_agent_file` | built-in | filesystem tools with read-only permissions on `/maef/agent` | file-read tool on `/maef/agent` |
| MCP servers | MAEF MCP containers | `langchain-mcp-adapters` to the same containers | `MCPClient` to the same containers |
| model settings | adapter | `ChatOpenAI(base_url=proxy, ...)` | `OpenAIModel(client_args={"base_url": proxy}, params=...)` |
| budget | budget guard | proxy (tokens, turns as model calls, time) | proxy |
| outputs | `/workspace/out/*` | same | same |

Each image has one entry point, `maef-harness-run`, that reads `/maef/run.json` (instruction, rendered system prompt,
skill paths, tool grants, model settings, harness options), builds the framework's agent, runs it once, writes
`/workspace/out/output.md`, and exits with a status the worker maps to a run ending. The worker records the harness
name, image digest and framework version in the run manifest.

## Packages

| # | Package | Work | Size | Closed when |
|---|---|---|---|---|
| H.1 | Model proxy | OpenAI-compatible `/v1/chat/completions` (streaming and not) over `ModelAdapter`, run-scoped key, budget enforcement, transcript and token events like the MAEF loop; reachable only on the run's internal network | M | MAEF's own loop can run through the proxy with identical events and token counts (tested with the fake model) |
| H.2 | Harness seam | `runtime.harness` and `harness_options` in the schema and lint; the worker provisions a harness container, writes `/maef/run.json`, mounts skills read-only, collects outputs, maps exit status; manifest fields | M | a stub harness image (a 20-line script calling the proxy) completes a run end to end |
| H.3 | Deep Agents adapter | `harnesses/deepagents/`: locked dependencies, image pinned by digest, `maef-harness-run` mapping the table above | M | `tdd-engineer` with `harness: deepagents` runs on the fake model; the TDD trace reads its transcript |
| H.4 | Strands adapter | `harnesses/strands/`: same for `strands-agents[openai]` | M | same check with `harness: strands` |
| H.5 | MCP for external harnesses | connect both adapters to MAEF's MCP containers on the internal network | S | `research-analyst` runs under each harness with the fetch MCP behind the allowlist |
| H.6 | Fair comparison | an experiment spec with three arms (maef, deepagents, strands), same model and seeds; report per harness | S | the spec lints and runs on the fake model; the paid run needs your go-ahead (about $1–2 on Inkling 2 mini) |

Order: H.1 → H.2 → H.3 and H.4 in parallel → H.5 → H.6. H.1–H.4 need the run manifest (2.1) and are safest after
the budget fixes (1.1, 1.2), because the proxy becomes the budget's enforcement point for external harnesses.
H.6 needs experiments (phase 3).

## Open points

- Strands' native Bedrock path skips the proxy. Supported only through the proxy at first, so measurement stays
  equal; a native mode could come later for "run my existing Strands agent as it is" (use case 2), clearly marked as
  measured differently.
- Deep Agents can stream LangGraph events with more detail (planning state, subagent tree). Worth recording as extra
  artifacts in H.3, not needed for the comparison.
- Framework versions move fast (Strands releases about weekly). Images are rebuilt by explicit pull requests, and the
  framework version is part of every run's manifest.
