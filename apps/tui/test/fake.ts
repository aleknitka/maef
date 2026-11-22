/** Tiny fake MAEF API for headless tests: canned JSON plus a controllable SSE stream. */
const enc = new TextEncoder()
const TERMINAL = ["task.succeeded", "task.failed", "task.cancelled"]

export const BUDGET = { max_tokens: 50000, max_duration_seconds: 600, max_turns: 20 }
export const mkRun = (id: string, over: Record<string, unknown> = {}) => ({
  id, status: "running", agent_id: "researcher", git_rev: null, dirty: false, definition_digest: "d", profile_digest: "p",
  effective_budget: BUDGET, reserved_tokens: 50000, tokens_used: null, terminal_reason: null,
  created_at: "2026-01-02T03:04:05", finished_at: null, cancel_requested: false, ...over,
})
export const mkEvent = (run_id: string, seq: number, type: string, payload: unknown = {}) => ({
  run_id, seq, type, ts: `2026-01-02T03:04:${String(seq).padStart(2, "0")}`, payload,
})

export function startFake() {
  const state = {
    agents: [
      { id: "researcher", display_name: "Researcher", description: "Researches topics on the web", budget: BUDGET },
      { id: "coder", display_name: "Coder", description: "Writes patches", budget: BUDGET },
    ] as any[],
    runs: [mkRun("run-aaaa1111")] as any[],
    events: {} as Record<string, any[]>,
    subs: {} as Record<string, Set<ReadableStreamDefaultController<Uint8Array>>>,
    results: {} as Record<string, string>,
    artifacts: {} as Record<string, { digest: string; size: number; mime: string; name: string }[]>,
    blobs: {} as Record<string, Uint8Array>,
    submitBodies: [] as any[],
    cancelCalls: [] as string[],
    eventRequests: [] as { lastEventId: string | null }[],
    submitError: null as null | { status: number; body: unknown },
    cancelError: null as null | { status: number; body: unknown },
    dropAfterFirst: false,
    /** When set, requests must carry `Authorization: Bearer <requireToken>` or get 401. */
    requireToken: null as null | string,
    authHeaders: [] as (string | null)[],
  }
  const frame = (ev: any) => enc.encode(`id: ${ev.seq}\nevent: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`)
  const json = (body: unknown, status = 200) => Response.json(body, { status })

  const server = Bun.serve({
    port: 0,
    idleTimeout: 0,
    async fetch(req) {
      const url = new URL(req.url)
      const p = url.pathname
      let m: RegExpMatchArray | null
      state.authHeaders.push(req.headers.get("authorization"))
      if (state.requireToken && req.headers.get("authorization") !== `Bearer ${state.requireToken}`)
        return json({ detail: "Missing, invalid or revoked API token." }, 401)
      if (p === "/api/v1/agents") return json(state.agents)
      if (p === "/api/v1/runs/preview" && req.method === "POST") {
        const b: any = await req.json()
        return json({ agent_id: b.agent_id, instruction: b.instruction, effective_budget: { ...BUDGET, ...(b.budget ?? {}) },
          model_profile: "local-llama", sandbox_profile: "none", skills: [], mcp_servers: [], status: "preview_only", message: "ok" })
      }
      if (p === "/api/v1/runs" && req.method === "POST") {
        const b: any = await req.json()
        state.submitBodies.push(b)
        if (state.submitError) return json({ detail: state.submitError.body }, state.submitError.status)
        const run = mkRun("run-new00001", { agent_id: b.agent_id })
        state.runs.unshift(run)
        return json({ run_id: run.id, status: "admitted" }, 202)
      }
      if (p === "/api/v1/runs") return json(state.runs)
      if ((m = p.match(/^\/api\/v1\/runs\/([^/]+)\/events$/))) {
        const id = m[1]!
        const last = req.headers.get("last-event-id")
        state.eventRequests.push({ lastEventId: last })
        const after = Number(last ?? url.searchParams.get("after") ?? 0)
        const stream = new ReadableStream<Uint8Array>({
          start(c) {
            let done = false
            for (const ev of (state.events[id] ?? []).filter((e) => e.seq > after)) {
              c.enqueue(frame(ev))
              if (TERMINAL.includes(ev.type)) done = true
              if (state.dropAfterFirst) { state.dropAfterFirst = false; c.close(); return }
            }
            if (done) { c.close(); return }
            c.enqueue(enc.encode(": keepalive\n\n"))
            ;(state.subs[id] ??= new Set()).add(c)
          },
        })
        return new Response(stream, { headers: { "Content-Type": "text/event-stream" } })
      }
      if ((m = p.match(/^\/api\/v1\/runs\/([^/]+)\/cancel$/))) {
        state.cancelCalls.push(m[1]!)
        if (state.cancelError) return json({ detail: state.cancelError.body }, state.cancelError.status)
        return json({ outcome: "requested" }, 202)
      }
      if ((m = p.match(/^\/api\/v1\/runs\/([^/]+)\/artifacts$/))) return json(state.artifacts[m[1]!] ?? [])
      if ((m = p.match(/^\/api\/v1\/runs\/([^/]+)$/))) {
        const r = state.runs.find((x) => x.id === m![1])
        return r ? json(r) : json({ detail: "Run not found" }, 404)
      }
      if ((m = p.match(/^\/api\/v1\/artifacts\/(.+)$/))) {
        const blob = state.blobs[decodeURIComponent(m[1]!)]
        return blob ? new Response(blob as unknown as BodyInit) : json({ detail: "Artifact not found" }, 404)
      }
      return json({ detail: "Not found" }, 404)
    },
  })

  return {
    state,
    url: `http://127.0.0.1:${server.port}`,
    /** Append an event and push it to live SSE subscribers. */
    push(runId: string, ev: any) {
      ;(state.events[runId] ??= []).push(ev)
      for (const c of state.subs[runId] ?? []) {
        c.enqueue(frame(ev))
        if (TERMINAL.includes(ev.type)) { c.close(); state.subs[runId]!.delete(c) }
      }
    },
    stop: () => server.stop(true),
  }
}
