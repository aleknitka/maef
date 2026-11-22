import { useEffect, useMemo, useRef, useState } from "react"
import { useKeyboard, useRenderer } from "@opentui/react"
import { RGBA, SyntaxStyle } from "@opentui/core"
import { basename, resolve } from "node:path"
import { ApiError, TERMINAL_STATUSES, type ArtifactRef, type Api, type RunEvent, type RunRecord } from "./api.ts"
import { oneLine, pad, safe, summarize } from "./text.ts"
import { type ScreenProps, C, statusColor, usePoll } from "./ui.tsx"

type Mode = "log" | "confirm-cancel" | "result" | "artifacts" | "path" | "confirm-overwrite"
const MAX_EVENTS = 1000

function eventLine(e: RunEvent): string {
  const t = typeof e.ts === "string" ? safe(e.ts).slice(11, 19) : ""
  return `${pad(String(e.seq), 4)} ${pad(t, 8)} ${pad(oneLine(e.type, 24), 24)} ${summarize(e.payload)}`
}

export function RunDetail({ api, runId, onError, clearError, typing, pollMs, retry, active, onBack }: ScreenProps & { api: Api; runId: string; active: boolean; onBack: () => void }) {
  const renderer = useRenderer()
  const [run, setRun] = useState<RunRecord | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [mode, setMode] = useState<Mode>("log")
  const [notice, setNotice] = useState("")
  const [result, setResult] = useState<string | null | undefined>(undefined)
  const [artifacts, setArtifacts] = useState<ArtifactRef[]>([])
  const [chosen, setChosen] = useState<ArtifactRef | null>(null)
  const [path, setPath] = useState("")
  const syntax = useMemo(
    () => SyntaxStyle.fromStyles({ default: { fg: RGBA.fromHex("#E6EDF3") }, "markup.heading.1": { fg: RGBA.fromHex("#58A6FF"), bold: true } }, renderer.nativeScene),
    [renderer],
  )
  typing.current = active && mode === "path"
  const terminal = run ? TERMINAL_STATUSES.includes(run.status) : false

  // header: poll while the run is live
  usePoll(async (alive) => {
    if (terminal) return
    try { const r = await api.run(runId); if (alive()) { setRun(r); clearError() } } catch (e) { if (alive()) onError(e) }
  }, pollMs, [runId, terminal, retry])

  // live events over SSE (reconnect + Last-Event-ID handled by api.events)
  useEffect(() => {
    const ctl = new AbortController()
    setEvents([])
    ;(async () => {
      try {
        for await (const ev of api.events(runId, { signal: ctl.signal })) {
          setEvents((prev) => [...prev, ev].slice(-MAX_EVENTS))
          if (String(ev.type).startsWith("task.")) api.run(runId).then(setRun).catch(() => {})
        }
      } catch (e) { if (!ctl.signal.aborted) onError(e) }
    })()
    return () => ctl.abort()
  }, [runId, retry])

  async function loadResult() {
    setMode("result"); setResult(undefined)
    try { setResult(await api.resultText(runId)); clearError() } catch (e) { onError(e); setResult(null) }
  }
  async function loadArtifacts() {
    try { setArtifacts(await api.artifacts(runId)); setMode("artifacts"); clearError() } catch (e) { onError(e) }
  }
  async function doCancel() {
    setMode("log")
    try {
      const r = await api.cancel(runId)
      clearError()
      setNotice(r.outcome === "requested" ? "Cancel requested; waiting for the worker." : "Run cancelled.")
      api.run(runId).then(setRun).catch(() => {})
    } catch (e) { onError(e) }
  }
  async function write(overwrite: boolean, p: string = path) {
    if (!chosen) return
    setPath(p)
    const target = resolve(p)
    try {
      if (!overwrite && (await Bun.file(target).exists())) { setMode("confirm-overwrite"); return }
      const bytes = await api.download(chosen.digest)
      await Bun.write(target, bytes)
      clearError()
      setNotice(`Saved ${bytes.length} bytes to ${target}`)
      setMode("artifacts")
    } catch (e) {
      onError(e instanceof ApiError ? e : new ApiError(0, "write_failed", `Could not write file: ${safe(e instanceof Error ? e.message : e)}`))
      setMode("artifacts")
    }
  }

  useKeyboard((key) => {
    if (!active) return
    if (mode === "log") {
      if (key.name === "c" && !terminal) setMode("confirm-cancel")
      else if (key.name === "r") void loadResult()
      else if (key.name === "a") void loadArtifacts()
      else if (key.name === "escape" || key.name === "b") onBack()
    } else if (mode === "confirm-cancel") {
      if (key.name === "y") void doCancel()
      else if (key.name === "n" || key.name === "escape") setMode("log")
    } else if (mode === "confirm-overwrite") {
      if (key.name === "y") void write(true)
      else if (key.name === "n" || key.name === "escape") setMode("artifacts")
    } else if (mode === "path") {
      if (key.name === "escape") setMode("artifacts")
    } else if (key.name === "escape" || key.name === "b") setMode(mode === "result" || mode === "artifacts" ? "log" : "artifacts")
  })

  const budget = run?.effective_budget
  const options = useMemo(
    () => artifacts.map((a) => ({ name: `${pad(oneLine(a.name, 30), 31)}${pad(`${a.size}B`, 10)}${oneLine(a.mime, 30)}`, description: "", value: a })),
    [artifacts],
  )

  return (
    <box flexDirection="column" flexGrow={1}>
      <text>
        <span fg={C.accent}>{`run ${oneLine(runId, 12)} `}</span>
        <span fg={statusColor(run?.status ?? "")}>{oneLine(run?.status ?? "…", 12)}</span>
        {`  agent=${oneLine(run?.agent_id ?? "-", 24)}  reason=${oneLine(run?.terminal_reason ?? "-", 40)}`}
      </text>
      <text fg={C.dim}>
        {`tokens ${run?.tokens_used ?? "-"}/${run?.reserved_tokens ?? "-"}  budget tokens=${budget?.max_tokens ?? "-"} turns=${budget?.max_turns ?? "-"} duration=${budget?.max_duration_seconds ?? "-"}s${run?.cancel_requested ? "  (cancel requested)" : ""}`}
      </text>
      {notice ? <text fg={C.ok}>{oneLine(notice, 200)}</text> : null}
      {mode === "confirm-cancel" ? <text fg={C.warn}>Cancel this run? y = yes, n = no</text> : null}
      {mode === "confirm-overwrite" ? <text fg={C.warn}>{`${oneLine(resolve(path), 150)} exists. Overwrite? y = yes, n = no`}</text> : null}

      {mode === "result" ? (
        <scrollbox flexGrow={1} focused={active} stickyStart="top">
          {result === undefined ? <text fg={C.dim}>Loading result…</text>
            : result === null ? <text fg={C.dim}>No result artifact (output.md) for this run.</text>
            : <markdown content={safe(result)} syntaxStyle={syntax} />}
        </scrollbox>
      ) : mode === "artifacts" || mode === "confirm-overwrite" || mode === "path" ? (
        <box flexDirection="column" flexGrow={1}>
          {artifacts.length === 0 ? <text fg={C.dim}>No artifacts.</text> : (
            <select
              flexGrow={1}
              options={options}
              showDescription={false}
              focused={active && mode === "artifacts"}
              onSelect={(i) => {
                const a = artifacts[i]
                if (!a) return
                setChosen(a)
                setPath(resolve(basename(safe(a.name).replace(/[\\/]/g, "_")) || "artifact.bin"))
                setNotice("")
                setMode("path")
              }}
            />
          )}
          {mode === "path" ? (
            <box title="Save to (Enter to save, Esc to cancel)" border height={3}>
              <input focused={active} value={path} onInput={setPath} onSubmit={(v: string) => { void write(false, v) }} />
            </box>
          ) : null}
        </box>
      ) : (
        <scrollbox flexGrow={1} stickyScroll stickyStart="bottom" focused={active}>
          {events.length === 0 ? <text fg={C.dim}>Waiting for events…</text> : events.map((e) => <text key={e.seq}>{eventLine(e)}</text>)}
        </scrollbox>
      )}
    </box>
  )
}
