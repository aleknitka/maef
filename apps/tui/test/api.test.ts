import { afterAll, afterEach, beforeEach, describe, expect, test } from "bun:test"
import { ApiError, createApi, errorFromResponse, type BudgetOverride, type RunEvent } from "../src/api.ts"
import { BUDGET, startFake } from "./fake.ts"

const enc = new TextEncoder()
const sha256 = (b: Uint8Array) => new Bun.CryptoHasher("sha256").update(b).digest("hex")

// Scripted server for events() and raw error bodies: each request gets the next queued reply.
type Reply = () => Response
const queue: Reply[] = []
const seenLastEventIds: (string | null)[] = []
const scripted = Bun.serve({
  port: 0,
  idleTimeout: 0,
  fetch(req) {
    if (new URL(req.url).pathname.endsWith("/events")) seenLastEventIds.push(req.headers.get("last-event-id"))
    const next = queue.shift()
    return next ? next() : new Response("no script queued", { status: 500 })
  },
})
const scriptedUrl = `http://127.0.0.1:${scripted.port}`
afterAll(() => scripted.stop(true))
beforeEach(() => { queue.length = 0; seenLastEventIds.length = 0 })

let fake: ReturnType<typeof startFake>
beforeEach(() => { fake = startFake() })
afterEach(() => fake.stop())

const frame = (seq: number, type: string, payload: unknown = {}) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ run_id: "run-1", seq, type, ts: "2026-01-02T03:04:05", payload })}\n\n`

/** A text/event-stream reply. close=false keeps the connection open after the chunks. */
const stream = (chunks: (string | Uint8Array)[], close = true): Reply => () =>
  new Response(new ReadableStream<Uint8Array>({
    start(c) {
      for (const ch of chunks) c.enqueue(typeof ch === "string" ? enc.encode(ch) : ch)
      if (close) c.close()
    },
  }), { headers: { "Content-Type": "text/event-stream" } })

async function drain(gen: AsyncGenerator<RunEvent>) {
  const events: RunEvent[] = []
  try {
    for await (const ev of gen) events.push(ev)
    return { events, seqs: events.map((e) => e.seq), error: null as ApiError | null }
  } catch (e) {
    return { events, seqs: events.map((e) => e.seq), error: e as ApiError }
  }
}

describe("error parsing", () => {
  test("detail as a string becomes the message with code http_error", () => {
    const e = errorFromResponse(409, JSON.stringify({ detail: "Agent is disabled." }))
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(409)
    expect(e.code).toBe("http_error")
    expect(e.message).toBe("Agent is disabled.")
    expect(e.display()).toBe("Agent is disabled. (HTTP 409, http_error)")
  })

  test("detail as {code, message} uses both", () => {
    const e = errorFromResponse(409, JSON.stringify({ detail: { code: "insufficient_budget", message: "Not enough token budget." } }))
    expect(e.code).toBe("insufficient_budget")
    expect(e.message).toBe("Not enough token budget.")
  })

  test("detail {code} without message falls back to the generic message", () => {
    const e = errorFromResponse(400, JSON.stringify({ detail: { code: "x" } }))
    expect(e.code).toBe("x")
    expect(e.message).toBe("Request failed.")
  })

  test("validation array detail becomes invalid_request without echoing the entries", () => {
    const e = errorFromResponse(422, JSON.stringify({ detail: [{ loc: ["body", "instruction"], msg: "secret field detail" }] }))
    expect(e.code).toBe("invalid_request")
    expect(e.message).toBe("The request was rejected as invalid.")
  })

  test("non-JSON body yields the generic message and never the raw body", () => {
    const e = errorFromResponse(500, "<html>Traceback (most recent call last) /srv/private/app.py</html>")
    expect(e.message).toBe("Request failed.")
    expect(e.code).toBe("http_error")
    expect(e.message).not.toContain("Traceback")
    expect(e.message).not.toContain("/srv")
  })

  test("message is capped at 300 characters", () => {
    const e = errorFromResponse(400, JSON.stringify({ detail: "a".repeat(500) }))
    expect(e.message.length).toBe(300)
  })

  test("control characters in the message are stripped", () => {
    const e = errorFromResponse(400, JSON.stringify({ detail: "bad\u0007msg\u0000end" }))
    expect(e.message).toBe("badmsgend")
  })

  test("an API error reaches the caller through createApi with its code", async () => {
    fake.state.submitError = { status: 409, body: { code: "no_account", message: "Link a project first." } }
    const api = createApi(fake.url, fetch, 5)
    const err = await api.submit("researcher", "go").catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe("no_account")
    expect(err.message).toBe("Link a project first.")
    expect(err.unreachable).toBe(false)
  })

  test("the API token is sent as a bearer header and a 401 explains how to get one", async () => {
    fake.state.requireToken = "maef_secret"
    try {
      const denied = await createApi(fake.url, fetch, 5, "").agents().catch((e) => e)
      expect(denied.status).toBe(401)
      expect(denied.message).toContain("MAEF_API_TOKEN")
      expect(await createApi(fake.url, fetch, 5, "maef_secret").agents()).toHaveLength(2)
      expect(fake.state.authHeaders.at(-1)).toBe("Bearer maef_secret")
    } finally { fake.state.requireToken = null }
  })

  test("a 500 with a non-JSON body surfaces through createApi as a generic error", async () => {
    queue.push(() => new Response("<pre>stack at /home/x/y.ts:1</pre>", { status: 500 }))
    const err = await createApi(scriptedUrl, fetch, 5).agents().catch((e) => e)
    expect(err.status).toBe(500)
    expect(err.message).toBe("Request failed.")
  })
})

describe("request bodies", () => {
  test("submit sends only the budget fields that were provided", async () => {
    await createApi(fake.url, fetch, 5).submit("researcher", "find it", { max_turns: 5 })
    const body = fake.state.submitBodies[0]
    expect(body).toEqual({ agent_id: "researcher", instruction: "find it", budget: { max_turns: 5 } })
    expect(Object.keys(body.budget)).toEqual(["max_turns"])
  })

  test("submit omits the budget key entirely when every field is undefined or null", async () => {
    // null is not in BudgetOverride; the client is expected to drop it at runtime too.
    const nullish = { max_tokens: undefined, max_turns: null } as unknown as BudgetOverride
    await createApi(fake.url, fetch, 5).submit("researcher", "x", nullish)
    expect("budget" in fake.state.submitBodies[0]).toBe(false)
  })

  test("submit drops null fields but keeps the provided ones", async () => {
    const nullish = { max_tokens: null, max_turns: 7 } as unknown as BudgetOverride
    await createApi(fake.url, fetch, 5).submit("researcher", "x", nullish)
    expect(Object.keys(fake.state.submitBodies[0].budget)).toEqual(["max_turns"])
  })

  test("preview returns the effective budget (override merged over the agent ceiling)", async () => {
    const p = await createApi(fake.url, fetch, 5).preview("researcher", "x", { max_duration_seconds: 30 })
    expect(p.effective_budget).toEqual({ ...BUDGET, max_duration_seconds: 30 })
    expect((p as unknown as { status: string }).status).toBe("preview_only") // not declared on RunPreview
    expect(p.model_profile).toBe("local-llama")
  })
})

describe("events()", () => {
  test("ignores keepalive comment lines", async () => {
    queue.push(stream([": keepalive\n\n", frame(1, "run.started"), ": keepalive\n\n", ": keepalive\n\n", frame(2, "task.succeeded")]))
    const r = await drain(createApi(scriptedUrl, fetch, 5).events("run-1"))
    expect(r.error).toBeNull()
    expect(r.seqs).toEqual([1, 2])
  })

  test("reassembles a frame delivered one byte at a time, including split UTF-8", async () => {
    const bytes = enc.encode(frame(1, "run.started", { msg: "café → ok" }) + frame(2, "task.failed"))
    queue.push(stream([...bytes].map((b) => new Uint8Array([b]))))
    const r = await drain(createApi(scriptedUrl, fetch, 5).events("run-1"))
    expect(r.seqs).toEqual([1, 2])
    expect((r.events[0]!.payload as { msg: string }).msg).toBe("café → ok")
  })

  test("accepts CRLF line endings", async () => {
    queue.push(stream([frame(1, "run.started").replace(/\n/g, "\r\n") + frame(2, "task.succeeded").replace(/\n/g, "\r\n")]))
    const r = await drain(createApi(scriptedUrl, fetch, 5).events("run-1"))
    expect(r.seqs).toEqual([1, 2])
  })

  test("de-duplicates a repeated seq after reconnect and sends Last-Event-ID", async () => {
    queue.push(stream([frame(1, "run.started"), frame(2, "usage.recorded")]))
    queue.push(stream([frame(2, "usage.recorded"), frame(3, "tool.call"), frame(4, "task.succeeded")]))
    const r = await drain(createApi(scriptedUrl, fetch, 5).events("run-1"))
    expect(r.error).toBeNull()
    expect(r.seqs).toEqual([1, 2, 3, 4])
    expect(seenLastEventIds).toEqual([null, "2"])
  })

  test("the after option is sent as Last-Event-ID on the first connection", async () => {
    queue.push(stream([frame(8, "task.succeeded")]))
    const r = await drain(createApi(scriptedUrl, fetch, 5).events("run-1", { after: 7 }))
    expect(seenLastEventIds).toEqual(["7"])
    expect(r.seqs).toEqual([8])
  })

  test("gives up with a connection error after 3 reconnect attempts (4 tries in total)", async () => {
    const dead = Bun.serve({ port: 0, fetch: () => new Response("") })
    const url = `http://127.0.0.1:${dead.port}`
    dead.stop(true)
    let calls = 0
    const api = createApi(url, ((input: any, init: any) => { calls++; return fetch(input, init) }) as typeof fetch, 5)
    const r = await drain(api.events("run-1"))
    expect(r.error?.code).toBe("connection_error")
    expect(r.error?.unreachable).toBe(true)
    expect(calls).toBe(4)
  })

  test("the failure count resets after a connection that delivered events", async () => {
    const plan = ["fail", "fail", "ok", "fail", "fail", "ok"]
    queue.push(stream([frame(1, "run.started")]))
    queue.push(stream([frame(2, "task.succeeded")]))
    let i = 0
    const flaky = ((input: any, init: any) => {
      const step = plan[i++]
      if (step === "fail") return Promise.reject(new TypeError("connection refused"))
      return fetch(input, init)
    }) as typeof fetch
    const r = await drain(createApi(scriptedUrl, flaky, 5).events("run-1"))
    expect(r.error).toBeNull()
    expect(r.seqs).toEqual([1, 2])
    expect(i).toBe(6)
  })

  for (const type of ["task.succeeded", "task.failed", "task.cancelled"]) {
    test(`stops at ${type} without waiting for the stream to close`, async () => {
      queue.push(stream([frame(1, "run.started"), frame(2, type, { tokens_used: 5 }), frame(3, "usage.recorded")], false))
      const r = await drain(createApi(scriptedUrl, fetch, 5).events("run-1"))
      expect(r.error).toBeNull()
      expect(r.seqs).toEqual([1, 2])
      expect(seenLastEventIds.length).toBe(1)
    })
  }
})

describe("resultText() and download()", () => {
  const run = "run-aaaa1111"

  test("resultText returns null when there is no output.md artifact", async () => {
    const body = enc.encode("notes")
    fake.state.artifacts[run] = [{ digest: "sha256:" + sha256(body), size: body.length, mime: "text/plain", name: "notes.txt" }]
    fake.state.blobs["sha256:" + sha256(body)] = body
    expect(await createApi(fake.url, fetch, 5).resultText(run)).toBeNull()
    expect(await createApi(fake.url, fetch, 5).resultText("run-without-artifacts")).toBeNull()
  })

  test("resultText returns the decoded output.md text", async () => {
    const body = enc.encode("# Done\n\nAnswer: 42\n")
    const digest = "sha256:" + sha256(body)
    fake.state.artifacts[run] = [{ digest, size: body.length, mime: "text/markdown", name: "output.md" }]
    fake.state.blobs[digest] = body
    expect(await createApi(fake.url, fetch, 5).resultText(run)).toBe("# Done\n\nAnswer: 42\n")
  })

  test("download returns bytes whose sha256 matches the digest", async () => {
    const body = enc.encode("hello artifact")
    const digest = "sha256:" + sha256(body)
    fake.state.blobs[digest] = body
    const got = await createApi(fake.url, fetch, 5).download(digest)
    expect(sha256(got)).toBe(sha256(body))
  })

  test("download throws digest_mismatch when the body does not match the digest", async () => {
    const expected = enc.encode("expected bytes")
    const digest = "sha256:" + sha256(expected)
    fake.state.blobs[digest] = enc.encode("tampered bytes")
    const err = await createApi(fake.url, fetch, 5).download(digest).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe("digest_mismatch")
    expect(err.unreachable).toBe(false)
  })

  test("resultText throws digest_mismatch on a tampered output.md", async () => {
    const digest = "sha256:" + sha256(enc.encode("original"))
    fake.state.artifacts[run] = [{ digest, size: 8, mime: "text/markdown", name: "output.md" }]
    fake.state.blobs[digest] = enc.encode("tampered")
    const err = await createApi(fake.url, fetch, 5).resultText(run).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe("digest_mismatch")
  })
})
