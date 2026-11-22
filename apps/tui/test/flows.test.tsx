import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { existsSync, mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { App } from "../src/App.tsx"
import { createApi } from "../src/api.ts"
import { mkEvent, mkRun, startFake } from "./fake.ts"

let fake: ReturnType<typeof startFake>
beforeEach(() => { fake = startFake() })
afterEach(() => fake.stop())

type Setup = Awaited<ReturnType<typeof testRender>>
const open: Setup[] = []
afterEach(() => { for (const s of open.splice(0)) s.renderer.destroy() })

// Same helpers as tui.test.tsx (copied so that file's tests are not registered twice).
async function mount(opts: { onQuit?: () => void } = {}, w = 110, h = 32) {
  const setup = await testRender(<App api={createApi(fake.url, fetch, 20)} pollMs={100} onQuit={opts.onQuit} />, { width: w, height: h })
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
/** Keep rendering for `ms` and return the last frame (for asserting that something did NOT happen). */
async function settle(setup: Setup, ms = 150): Promise<string> {
  const end = Date.now() + ms
  while (Date.now() < end) { await setup.renderOnce(); await Bun.sleep(10) }
  return setup.captureCharFrame()
}
const detailOf = async (s: Setup) => {
  await until(s, "Researcher")
  s.mockInput.pressKey("3")
  await until(s, "run-aaaa")
  s.mockInput.pressEnter()
  return until(s, "run run-aaaa")
}
const openNewRun = async (s: Setup) => {
  await until(s, "Researcher")
  s.mockInput.pressEnter()
  await until(s, "Agent: researcher")
}

describe("new run validation", () => {
  test("non-numeric or zero budget fields show an error and are not submitted", async () => {
    const s = await mount()
    await openNewRun(s)
    await s.mockInput.typeText("go")
    s.mockInput.pressTab()
    await until(s, "Max tokens *")

    await s.mockInput.typeText("abc")
    s.mockInput.pressKey("s", { ctrl: true })
    await until(s, "max_tokens must be a positive whole number.")
    expect(fake.state.submitBodies).toEqual([])

    for (let i = 0; i < 3; i++) s.mockInput.pressBackspace()
    await s.mockInput.typeText("0")
    s.mockInput.pressKey("s", { ctrl: true })
    await settle(s)
    expect(fake.state.submitBodies).toEqual([])

    s.mockInput.pressBackspace()
    await s.mockInput.typeText("100")
    s.mockInput.pressTab()
    await until(s, "Max turns *")
    await s.mockInput.typeText("2.5")
    s.mockInput.pressKey("s", { ctrl: true })
    await until(s, "max_turns must be a positive whole number.")
    expect(fake.state.submitBodies).toEqual([])

    // A corrected value submits, and only that submission reaches the API.
    for (let i = 0; i < 3; i++) s.mockInput.pressBackspace()
    await s.mockInput.typeText("3")
    s.mockInput.pressKey("s", { ctrl: true })
    await until(s, "run run-new0")
    expect(fake.state.submitBodies).toEqual([{ agent_id: "researcher", instruction: "go", budget: { max_tokens: 100, max_turns: 3 } }])
  })
})

describe("cancel", () => {
  test("answering n to the cancel prompt does not call the cancel route", async () => {
    const s = await mount()
    await detailOf(s)
    s.mockInput.pressKey("c")
    await until(s, "Cancel this run?")
    s.mockInput.pressKey("n")
    const f = await settle(s)
    expect(f).not.toContain("Cancel this run?")
    expect(fake.state.cancelCalls).toEqual([])
  })

  test("a 409 already_terminal response shows the API message and code", async () => {
    fake.state.cancelError = { status: 409, body: { code: "already_terminal", message: "Run already finished." } }
    const s = await mount()
    await detailOf(s)
    s.mockInput.pressKey("c")
    await until(s, "Cancel this run?")
    s.mockInput.pressKey("y")
    const f = await until(s, "Run already finished.")
    expect(f).toContain("already_terminal")
    expect(f).toContain("HTTP 409")
    expect(f).not.toContain("Cancel requested")
  })
})

describe("terminal run detail", () => {
  test("shows terminal reason and tokens, lists all events, then stops streaming", async () => {
    const id = "run-aaaa1111"
    fake.state.runs = [mkRun(id, { status: "failed", tokens_used: 1234, terminal_reason: "budget_exhausted", finished_at: "2026-01-02T03:05:00" })]
    fake.state.events[id] = [
      mkEvent(id, 1, "run.started", { agent: "researcher" }),
      mkEvent(id, 2, "usage.recorded", { turn: 1, total_tokens: 1234 }),
      mkEvent(id, 3, "task.failed", { tokens_used: 1234, terminal_reason: "budget_exhausted" }),
    ]
    const s = await mount()
    await detailOf(s)
    const f = await until(s, "task.failed")
    expect(f).toContain("reason=budget_exhausted")
    expect(f).toContain("tokens 1234/50000")
    expect(f).toContain("run.started")
    expect(f).toContain("usage.recorded")
    expect(f).toContain("total_tokens=1234")
    await settle(s, 250)
    expect(fake.state.eventRequests.length).toBe(1)
  })
})

describe("keyboard isolation while typing", () => {
  test("typing q or 1 in the instruction textarea does not quit or switch tabs", async () => {
    let quits = 0
    const s = await mount({ onQuit: () => { quits++ } })
    await openNewRun(s)
    await s.mockInput.typeText("q1 quit")
    const f = await until(s, "q1 quit")
    expect(quits).toBe(0)
    expect(f).toContain("Agent: researcher")
    expect(f).not.toContain("CREATED")
    expect(f).not.toContain("Researches topics")
  })
})

describe("runs table", () => {
  test("refresh picks up a status change from the API", async () => {
    fake.state.runs = [mkRun("run-aaaa1111", { status: "running" })]
    const s = await mount()
    await until(s, "Researcher")
    s.mockInput.pressKey("3")
    await until(s, "running")
    fake.state.runs = [mkRun("run-aaaa1111", { status: "succeeded", tokens_used: 77 })]
    const f = await until(s, "succeeded")
    expect(f).toContain("77/50000")
    expect(f).not.toContain("running")
  })
})

describe("artifact save", () => {
  // Runs from a temp working directory so nothing is ever written into the repo.
  test("a body that does not match the digest is not written to disk", async () => {
    const run = "run-aaaa1111"
    fake.state.runs = [mkRun(run, { status: "succeeded" })]
    const claimed = new Bun.CryptoHasher("sha256").update(new TextEncoder().encode("original")).digest("hex")
    const digest = "sha256:" + claimed
    fake.state.artifacts[run] = [{ digest, size: 8, mime: "text/plain", name: "notes.txt" }]
    fake.state.blobs[digest] = new TextEncoder().encode("tampered")
    const dir = mkdtempSync(join(tmpdir(), "maef-tui-"))
    const target = join(dir, "out.txt")
    const prevCwd = process.cwd()
    process.chdir(dir)
    try {
      const s = await mount()
      await detailOf(s)
      s.mockInput.pressKey("a")
      await until(s, "notes.txt")
      s.mockInput.pressEnter()
      await until(s, "Save to")
      await s.renderOnce()
      for (let i = 0; i < 300; i++) s.mockInput.pressBackspace()
      await s.mockInput.typeText(target)
      s.mockInput.pressEnter()
      const f = await until(s, "Download failed verification")
      expect(f).not.toContain("API unreachable")
      expect(existsSync(target)).toBe(false)
      expect(existsSync(join(dir, "notes.txt"))).toBe(false)
    } finally {
      process.chdir(prevCwd)
    }
  })

  test("a write failure shows its own message, not the unreachable hint", async () => {
    const run = "run-aaaa1111"
    fake.state.runs = [mkRun(run, { status: "succeeded" })]
    const body = new TextEncoder().encode("hello")
    const digest = "sha256:" + new Bun.CryptoHasher("sha256").update(body).digest("hex")
    fake.state.artifacts[run] = [{ digest, size: body.length, mime: "text/plain", name: "notes.txt" }]
    fake.state.blobs[digest] = body
    const dir = mkdtempSync(join(tmpdir(), "maef-tui-"))
    const prevCwd = process.cwd()
    process.chdir(dir)
    try {
      const s = await mount()
      await detailOf(s)
      s.mockInput.pressKey("a")
      await until(s, "notes.txt")
      s.mockInput.pressEnter()
      await until(s, "Save to")
      await s.renderOnce()
      for (let i = 0; i < 300; i++) s.mockInput.pressBackspace()
      await s.mockInput.typeText(dir) // a directory: cannot be written as a file
      s.mockInput.pressEnter()
      const f = await until(s, "Could not write file")
      expect(f).not.toContain("API unreachable")
    } finally {
      process.chdir(prevCwd)
    }
  })
})
