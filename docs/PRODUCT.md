# Product requirements — MAEF

## Purpose
MAEF is a test bed for controlled, repeatable experiments on AI agents: change one thing (model, prompt, skills, tools, settings), keep everything else fixed, run each version in a sealed environment across many seeds, measure outcome, process, cost and safety, and save each experiment as a FAIR record. The agent definitions it produces are self-contained, so the winning version can be released and run as a specialist on its own (CLI, Claude Code/Codex gateway, bundle). See [ROADMAP.md](ROADMAP.md).

The original aim still holds for released specialists: a reusable, reviewable, Git-native environment for delegating bounded work from general coding assistants, without loading specialist contexts and MCP servers into those assistants.

## Actors
- **Author:** defines agent capabilities, instructions, skills, tool grants, allowed model profiles, acceptance tests.
- **Reviewer:** diffs definitions, validates provenance and policies, approves promotion.
- **Operator:** registers workers, allocates quotas, inspects runs and costs, terminates work.
- **Caller:** human, CLI, Codex/Claude Code MCP client or workflow that submits task requests.
- **Worker:** authenticated executor that runs harnesses inside isolated environments.

## Studio information architecture
1. **Overview:** recent executions, active workers, spend and failure summaries.
2. **Agents:** search, compare versions, view prompts/context, clone, draft, review, publish.
3. **Skills & Tools:** review skills, resolved dependencies, MCP manifests, capability/permission requests.
4. **Models:** endpoint configurations, model capabilities and approved pricing profiles (never reveal credentials).
5. **Runs:** task input, timeline, budget reserve/consumption, traces, output artefacts, cancellation.
6. **Workers:** local and remote status, sandbox capabilities, resource/queue utilisation.
7. **Policies:** permissions, network allowlists, quota ceilings, approvals.
8. **Evaluations:** golden tasks, regression comparisons, quality and cost/performance.

## Primary workflow
Author drafts agent -> schema/security validation -> reviewer approves Git PR -> registry indexes commit -> caller previews delegation -> policy admission -> dispatcher reserves budget and selects worker -> worker resolves immutable agent revision -> sandbox starts harness -> artifacts and telemetry streamed -> validators execute -> result stored -> operator reviews and merges change via explicit approval.

## Initial acceptance criteria
- A YAML definition can be loaded and validated.
- Studio lists and displays specialist settings, instructions, skills and expected outputs.
- Caller can preview task-specific effective limits without executing tools.
- Unknown IDs and path traversal do not expose files.
- API indicates clearly that execution is not available in bootstrap.

## Later acceptance criteria
- Running a task uses pinned agent/configuration and model profile revisions.
- A worker obtains only the capabilities explicitly granted to that task.
- Budget reservations prevent concurrent overspending within defined tolerances.
- A terminated run's secrets are revoked and sandbox destroyed.
- Rendered output is verified and source revision is retained independently of runtime logs.
- Runner reports accurate used tokens and known versus estimated financial amounts.

## Non-goals for v1
No agent marketplace, automatic credential discovery, unapproved internet access, privileged host mounts, multi-tenant billing, Kubernetes dependency or full autonomous branching DAG scheduler.

## Questions to settle during implementation
Credential store for local machine; Windows/macOS worker support versus Linux VM; API-model versus subscription CLI usage; task cost policy for unpriced models; approval granularity; long-term artifact retention; and trust policy for third-party MCP/skill packages.
