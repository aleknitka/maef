import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { existsSync, mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { App } from "../src/App.tsx"
import { createApi, type RunEvent } from "../src/api.ts"
import { safe, summarize } from "../src/text.ts"
import { mkEvent, mkRun, startFake } from "./fake.ts"

let fake: ReturnType<typeof startFake>
beforeEach(() => { fake = startFake() })
afterEach(() => fake.stop())

type Setup = Awaited<ReturnType<typeof testRender>>
const open: Setup[] = []
afterEach(() => { for (const s of open.splice(0)) s.renderer.destroy() })

async function mount(url = fake.url, w = 110, h = 32) {
  const setup = await testRender(<App api={createApi(url, fetch, 20)} pollMs={100} />, { width: w, height: h })
  ;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = false
  open.push(setup)
  return setup
}
/** Render until the frame contains `text` (network results arrive outside the render scheduler). */
async function until(setup: Setup, text: string, ms = 3000): Promise<string> {
  const end = Date.now() + ms
  let frame = ""
  while (Date.now() < end) {
    await setup.renderOnce()
    frame = setup.captureCharFrame()
    if (frame.includes(text)) return frame
    await Bun.sleep(15)
  }
  throw new Error(`Timed out waiting for ${JSON.stringify(text)}. Last frame:\n${frame}`)
}
const detailOf = async (s: Setup) => {
  await until(s, "Researcher")
  s.mockInput.pressKey("3")
  await until(s, "run-aaaa")
  s.mockInput.pressEnter()
  return until(s, "run run-aaaa")
}

describe("screens", () => {
  test("agents list renders id, name and description", async () => {
    const s = await mount()
    const f = await until(s, "Researches topics")
    expect(f).toContain("researcher")
    expect(f).toContain("Coder")
    expect(f).toContain("Writes patches")
    expect(f).toContain("1 Agents")
    expect(f).toContain("Enter new run")
  })

  test("submitting a run navigates to run detail with budget override", async () => {
    const s = await mount()
    await until(s, "Researcher")
    s.mockInput.pressEnter()
    await until(s, "Agent: researcher")
    await s.mockInput.typeText("find the answer")
    s.mockInput.pressTab()
    await until(s, "Max tokens *")
    await s.mockInput.typeText("1234")
    s.mockInput.pressKey("s", { ctrl: true })
    await until(s, "run run-new0")
    expect(fake.state.submitBodies).toEqual([{ agent_id: "researcher", instruction: "find the answer", budget: { max_tokens: 1234 } }])
  })

  test("preview shows the effective budget", async () => {
    const s = await mount()
    await until(s, "Researcher")
    s.mockInput.pressEnter()
    await until(s, "Agent: researcher")
    await s.mockInput.typeText("hello")
    s.mockInput.pressKey("p", { ctrl: true })
    const f = await until(s, "Preview (nothing submitted)")
    expect(f).toContain("tokens=50000")
    expect(f).toContain("local-llama")
  })

  test("runs table shows tokens and status", async () => {
    fake.state.runs = [mkRun("run-aaaa1111", { status: "succeeded", tokens_used: 321 })]
    const s = await mount()
    await until(s, "Researcher")
    s.mockInput.pressKey("3")
    const f = await until(s, "run-aaaa")
    expect(f).toContain("succeeded")
    expect(f).toContain("321/50000")
  })

  test("live events appear as they are pushed", async () => {
    fake.state.events["run-aaaa1111"] = [mkEvent("run-aaaa1111", 1, "run.started", { agent: "researcher" })]
    const s = await mount()
    await detailOf(s)
    await until(s, "run.started")
    fake.push("run-aaaa1111", mkEvent("run-aaaa1111", 2, "usage.recorded", { turn: 1, total_tokens: 99 }))
    const f = await until(s, "usage.recorded")
    expect(f).toContain("total_tokens=99")
  })

  test("cancel asks for confirmation and calls the cancel route", async () => {
    const s = await mount()
    await detailOf(s)
    s.mockInput.pressKey("c")
    await until(s, "Cancel this run?")
    expect(fake.state.cancelCalls).toEqual([])
    s.mockInput.pressKey("y")
    await until(s, "Cancel requested")
    expect(fake.state.cancelCalls).toEqual(["run-aaaa1111"])
  })

  test("result view renders output.md as markdown", async () => {
    fake.state.runs = [mkRun("run-aaaa1111", { status: "succeeded" })]
    const body = new TextEncoder().encode("# Findings\n\nThe answer is **42**.\n")
    const d1 = "sha256:" + new Bun.CryptoHasher("sha256").update(body).digest("hex")
    fake.state.artifacts["run-aaaa1111"] = [{ digest: d1, size: body.length, mime: "text/markdown", name: "output.md" }]
    fake.state.blobs[d1] = body
    const s = await mount()
    await detailOf(s)
    s.mockInput.pressKey("r")
    const f = await until(s, "Findings")
    expect(f).toContain("The answer is")
    expect(f).not.toContain("**")
  })

  test("artifact download refuses overwrite until confirmed", async () => {
    fake.state.runs = [mkRun("run-aaaa1111", { status: "succeeded" })]
    const body = new TextEncoder().encode("hello artifact")
    const digest = "sha256:" + new Bun.CryptoHasher("sha256").update(body).digest("hex")
    fake.state.artifacts["run-aaaa1111"] = [{ digest, size: body.length, mime: "text/plain", name: "notes.txt" }]
    fake.state.blobs[digest] = body
    const dir = mkdtempSync(join(tmpdir(), "maef-tui-"))
    const target = join(dir, "out.txt")
    writeFileSync(target, "old") // pre-existing file the save must not clobber silently
    const prevCwd = process.cwd()
    process.chdir(dir) // nothing may leak into the repo
    try {
      const s = await mount()
      await detailOf(s)
      s.mockInput.pressKey("a")
      await until(s, "notes.txt")
      s.mockInput.pressEnter()
      await until(s, "Save to")
      await s.renderOnce()
      // replace the prefilled path
      for (let i = 0; i < 300; i++) s.mockInput.pressBackspace()
      await s.mockInput.typeText(target)
      s.mockInput.pressEnter()
      await until(s, "Overwrite?")
      expect(await Bun.file(target).text()).toBe("old")
      s.mockInput.pressKey("y")
      await until(s, "Saved 14 bytes")
      expect(await Bun.file(target).text()).toBe("hello artifact")
      expect(existsSync(join(dir, "notes.txt"))).toBe(false)
    } finally {
      process.chdir(prevCwd)
    }
  })
})

describe("untrusted text and errors", () => {
  test("control characters in event payloads are stripped", async () => {
    fake.state.events["run-aaaa1111"] = [
      mkEvent("run-aaaa1111", 1, "tool.result\u001b]0;pwn\u0007", { name: "\u001b[2Jevil\u0000\u0007x\ry" }),
    ]
    const s = await mount()
    await detailOf(s)
    const f = await until(s, "tool.result")
    expect(f).not.toMatch(/[\u0000-\u0008\u000B-\u001F\u007F]/)
    expect(f).toContain("evilxy")
    expect(f).not.toContain("\u001b")
  })

  test("safe() keeps newline and tab only", () => {
    expect(safe("a\nb\tc\u001b[0m\u0000\u009b\u007f")).toBe("a\nb\tc")
    expect(summarize({ a: 1, b: { c: "\u001b" } })).toBe("a=1 b={\"c\":\"\\u001b\"}")
  })

  test("API error message and code are shown", async () => {
    fake.state.submitError = { status: 409, body: { code: "insufficient_budget", message: "Not enough token budget." } }
    const s = await mount()
    await until(s, "Researcher")
    s.mockInput.pressEnter()
    await until(s, "Agent: researcher")
    await s.mockInput.typeText("go")
    s.mockInput.pressKey("s", { ctrl: true })
    const f = await until(s, "Not enough token budget.")
    expect(f).toContain("insufficient_budget")
    expect(f).toContain("HTTP 409")
    expect(f).not.toContain("at ")
  })

  test("unreachable API shows retry hint", async () => {
    const dead = startFake()
    const url = dead.url
    dead.stop()
    const s = await mount(url)
    const f = await until(s, "API unreachable")
    expect(f).toContain("ctrl+r")
  })
})

describe("api client", () => {
  test("events() resumes with Last-Event-ID and stops at the terminal event", async () => {
    const id = "run-aaaa1111"
    fake.state.events[id] = [mkEvent(id, 1, "run.started"), mkEvent(id, 2, "usage.recorded"), mkEvent(id, 3, "task.succeeded", { tokens_used: 5 })]
    fake.state.dropAfterFirst = true
    const seen: RunEvent[] = []
    for await (const ev of createApi(fake.url, fetch, 5).events(id)) seen.push(ev)
    expect(seen.map((e) => e.seq)).toEqual([1, 2, 3])
    expect(fake.state.eventRequests.map((r) => r.lastEventId)).toEqual([null, "1"])
  })
})
