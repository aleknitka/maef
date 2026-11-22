/** The only module that knows MAEF API URLs. Mirrors apps/api/maef_api/client.py semantics. */
import { safe } from "./text.ts"

export const DEFAULT_URL = "http://127.0.0.1:8000"
export const TERMINAL_STATUSES = ["succeeded", "failed", "cancelled"]
export const TERMINAL_EVENTS = ["task.succeeded", "task.failed", "task.cancelled"]
export const RESULT_NAME = "output.md"
const MAX_MESSAGE = 300
const AUTH_HINT = "Set MAEF_API_TOKEN (create one with `maef admin token create`)."
const MAX_RECONNECTS = 3

export interface Budget { max_tokens: number; max_duration_seconds: number; max_turns: number }
export type BudgetOverride = Partial<Budget>
export interface Agent {
  id: string; version?: string | number; display_name: string; description: string
  runtime?: { model_profile: string; sandbox_profile: string }; budget?: Budget
}
export interface RunPreview {
  agent_id: string; effective_budget: Budget; model_profile: string; sandbox_profile: string
  skills: string[]; mcp_servers: string[]; message: string
}
export interface RunRecord {
  id: string; status: string; agent_id: string; effective_budget: Budget; reserved_tokens: number
  tokens_used: number | null; terminal_reason: string | null; created_at: string
  finished_at: string | null; cancel_requested: boolean
}
export interface RunEvent { seq: number; type: string; ts: string; payload: unknown }
export interface ArtifactRef { digest: string; size: number; mime: string; name: string }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message)
  }
  get unreachable(): boolean { return this.code === "connection_error" }
  /** User-facing text: the API's own message and code only, never a stack. */
  display(): string {
    return this.status ? `${this.message} (HTTP ${this.status}, ${this.code})` : `${this.message} (${this.code})`
  }
}

export function errorFromResponse(status: number, body: string): ApiError {
  let code = "http_error"
  let message = "Request failed."
  try {
    const detail = JSON.parse(body)?.detail
    if (typeof detail === "string") message = detail
    else if (Array.isArray(detail)) { code = "invalid_request"; message = "The request was rejected as invalid." }
    else if (detail && typeof detail === "object") {
      code = String(detail.code || code)
      message = String(detail.message || message)
    }
  } catch { /* keep defaults */ }
  if (status === 401 || status === 403) message = `${safe(message).slice(0, MAX_MESSAGE - AUTH_HINT.length - 1)} ${AUTH_HINT}`
  return new ApiError(status, safe(code), safe(message).slice(0, MAX_MESSAGE))
}

/** Verify `bytes` against a `sha256:<hex>` digest; throws digest_mismatch otherwise. */
function verifyDigest(digest: string, bytes: Uint8Array): void {
  const want = /^sha256:([0-9a-f]{64})$/i.exec(digest)?.[1]?.toLowerCase()
  if (!want || new Bun.CryptoHasher("sha256").update(bytes).digest("hex") !== want)
    throw new ApiError(0, "digest_mismatch", "Download failed verification: bytes do not match the artifact digest.")
}

const unreachable = () => new ApiError(0, "connection_error", "Cannot reach the MAEF API.")

export interface Api {
  readonly baseUrl: string
  agents(): Promise<Agent[]>
  preview(agentId: string, instruction: string, budget?: BudgetOverride): Promise<RunPreview>
  submit(agentId: string, instruction: string, budget?: BudgetOverride): Promise<{ run_id: string; status: string }>
  runs(): Promise<RunRecord[]>
  run(id: string): Promise<RunRecord>
  cancel(id: string): Promise<{ outcome: string }>
  artifacts(id: string): Promise<ArtifactRef[]>
  download(digest: string): Promise<Uint8Array>
  resultText(id: string): Promise<string | null>
  events(id: string, opts?: { after?: number; signal?: AbortSignal }): AsyncGenerator<RunEvent>
}

/** Parse SSE lines into events; yields null for keepalive comments. */
export async function* frames(lines: AsyncIterable<string>): AsyncGenerator<RunEvent | null> {
  let fields: Record<string, string[]> = {}
  for await (const line of lines) {
    if (line === "") {
      if (fields.data) {
        const ev = decode(fields.data.join("\n"), fields)
        if (ev) yield ev
      }
      fields = {}
    } else if (line.startsWith(":")) yield null
    else {
      const i = line.indexOf(":")
      const name = i < 0 ? line : line.slice(0, i)
      let value = i < 0 ? "" : line.slice(i + 1)
      if (value.startsWith(" ")) value = value.slice(1)
      ;(fields[name] ??= []).push(value)
    }
  }
}

function decode(data: string, fields: Record<string, string[]>): RunEvent | null {
  let ev: any
  try { ev = JSON.parse(data) } catch { return null }
  if (!ev || typeof ev !== "object" || Array.isArray(ev)) return null
  const id = fields.id?.at(-1)
  if (ev.seq === undefined && id && /^\d+$/.test(id)) ev.seq = Number(id)
  if (ev.type === undefined && fields.event) ev.type = fields.event.at(-1)
  return ev as RunEvent
}

async function* lineIterator(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buf = ""
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let i: number
      while ((i = buf.search(/\r\n|\n|\r/)) >= 0) {
        const len = buf.startsWith("\r\n", i) ? 2 : 1
        if (buf[i] === "\r" && i === buf.length - 1) break // may be half of \r\n
        yield buf.slice(0, i)
        buf = buf.slice(i + len)
      }
    }
    if (buf) yield buf.replace(/\r$/, "")
  } finally {
    reader.cancel().catch(() => {})
  }
}

export function createApi(baseUrl: string = DEFAULT_URL, fetchImpl: typeof fetch = fetch, retryDelayMs = 500,
  token: string | undefined = process.env.MAEF_API_TOKEN): Api {
  const base = baseUrl.replace(/\/+$/, "")
  const auth: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {}
  const enc = encodeURIComponent

  async function send(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<Response> {
    let res: Response
    try {
      res = await fetchImpl(base + path, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json", ...auth, ...headers } : { ...auth, ...headers },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(30_000),
      })
    } catch {
      throw unreachable()
    }
    if (res.status >= 300) throw errorFromResponse(res.status, await res.text().catch(() => ""))
    return res
  }
  async function json<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await send(method, path, body)
    try { return (await res.json()) as T } catch { throw new ApiError(res.status, "bad_response", "The API returned a non-JSON response.") }
  }
  const runBody = (agent_id: string, instruction: string, budget?: BudgetOverride) => {
    const b: BudgetOverride = {}
    for (const [k, v] of Object.entries(budget ?? {})) if (v !== undefined && v !== null) (b as any)[k] = v
    return Object.keys(b).length ? { agent_id, instruction, budget: b } : { agent_id, instruction }
  }

  const api: Api = {
    baseUrl: base,
    agents: () => json("GET", "/api/v1/agents"),
    preview: (a, i, b) => json("POST", "/api/v1/runs/preview", runBody(a, i, b)),
    submit: (a, i, b) => json("POST", "/api/v1/runs", runBody(a, i, b)),
    runs: () => json("GET", "/api/v1/runs"),
    run: (id) => json("GET", `/api/v1/runs/${enc(id)}`),
    cancel: (id) => json("POST", `/api/v1/runs/${enc(id)}/cancel`),
    artifacts: (id) => json("GET", `/api/v1/runs/${enc(id)}/artifacts`),
    async download(digest) {
      const res = await send("GET", `/api/v1/artifacts/${enc(digest)}`)
      const bytes = new Uint8Array(await res.arrayBuffer())
      verifyDigest(digest, bytes)
      return bytes
    },
    async resultText(id) {
      const ref = (await api.artifacts(id)).find((r) => r.name === RESULT_NAME)
      if (!ref) return null
      return new TextDecoder("utf-8").decode(await api.download(ref.digest))
    },
    async *events(id, { after = 0, signal } = {}) {
      let cursor = after
      let failures = 0
      const path = `/api/v1/runs/${enc(id)}/events`
      const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
      while (!signal?.aborted) {
        let progressed = false
        try {
          let res: Response
          try {
            res = await fetchImpl(base + path, {
              headers: { Accept: "text/event-stream", ...auth, ...(cursor ? { "Last-Event-ID": String(cursor) } : {}) },
              signal,
            })
          } catch {
            if (signal?.aborted) return
            throw unreachable()
          }
          if (res.status >= 300) throw errorFromResponse(res.status, await res.text().catch(() => ""))
          if (!res.body) throw unreachable()
          try {
            for await (const frame of frames(lineIterator(res.body))) {
              if (!frame) continue
              if (typeof frame.seq === "number") {
                if (frame.seq <= cursor) continue
                cursor = frame.seq
              }
              progressed = true
              yield frame
              if (TERMINAL_EVENTS.includes(frame.type)) return
            }
          } catch (e) {
            if (signal?.aborted) return
            if (e instanceof ApiError) throw e
            // dropped stream: resume from the cursor
          }
          failures = 0
        } catch (e) {
          if (!(e instanceof ApiError) || e.status !== 0) throw e
          if (++failures > MAX_RECONNECTS) throw e
          await sleep(Math.min(failures, 3) * retryDelayMs)
          continue
        }
        if (signal?.aborted) return
        if (!progressed) {
          const run = await api.run(id)
          if (TERMINAL_STATUSES.includes(run.status)) return
          await sleep(200)
        }
      }
    },
  }
  return api
}
