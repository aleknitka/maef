import { useRef, useState } from "react"
import { useKeyboard } from "@opentui/react"
import type { Agent, Api } from "./api.ts"
import { ApiError } from "./api.ts"
import { Agents } from "./Agents.tsx"
import { NewRun } from "./NewRun.tsx"
import { RunDetail } from "./RunDetail.tsx"
import { Runs } from "./Runs.tsx"
import { oneLine } from "./text.ts"
import { C, errorText } from "./ui.tsx"

type Screen = "agents" | "new" | "runs" | "detail"
const TABS: [Screen, string][] = [["agents", "1 Agents"], ["new", "2 New run"], ["runs", "3 Runs"]]

const HINTS: Record<Screen, string> = {
  agents: "↑/↓ move  Enter new run  1/2/3 switch  ctrl+r retry  q quit",
  new: "Tab next field  ctrl+p preview  ctrl+s submit  Esc back  ctrl+c quit",
  runs: "↑/↓ move  Enter open  1/2/3 switch  ctrl+r retry  q quit",
  detail: "c cancel  r result  a artifacts  Esc/b back  1/2/3 switch  ctrl+r retry  q quit",
}

export function App({ api, pollMs = 2000, onQuit }: { api: Api; pollMs?: number; onQuit?: () => void }) {
  const [screen, setScreen] = useState<Screen>("agents")
  const [agent, setAgent] = useState<Agent | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [retry, setRetry] = useState(0)
  const typing = useRef(false)

  const onError = (e: unknown) => setError(e ?? new Error("invalid"))
  const clearError = () => setError(null)
  const go = (s: Screen) => { typing.current = false; setScreen(s) }
  const common = { onError, clearError, typing, pollMs, retry }

  useKeyboard((key) => {
    if (key.ctrl && key.name === "c") { onQuit?.(); return }
    if (key.ctrl && key.name === "r") { setError(null); setRetry((n) => n + 1); return }
    if (screen === "new" && key.name === "escape") { go("agents"); return }
    if (typing.current) return
    if (key.name === "q") onQuit?.()
    else if (key.name === "1") go("agents")
    else if (key.name === "2") go("new")
    else if (key.name === "3") go("runs")
  })

  const unreachable = error instanceof ApiError && error.unreachable
  return (
    <box flexDirection="column" width="100%" height="100%">
      <text>
        {TABS.map(([id, label]) => (
          <span key={id} fg={screen === id || (screen === "detail" && id === "runs") ? C.accent : C.dim}>{` ${label} `}</span>
        ))}
        <span fg={C.dim}>{`  ${oneLine(api.baseUrl, 60)}`}</span>
      </text>
      <box flexDirection="column" flexGrow={1}>
        {screen === "agents" ? <Agents {...common} api={api} active onPick={(a) => { setAgent(a); go("new") }} /> : null}
        {screen === "new" ? <NewRun {...common} api={api} agent={agent} active onSubmitted={(id) => { setRunId(id); go("detail") }} /> : null}
        {screen === "runs" ? <Runs {...common} api={api} active onOpen={(id) => { setRunId(id); go("detail") }} /> : null}
        {screen === "detail" && runId ? <RunDetail key={runId} {...common} api={api} runId={runId} active onBack={() => go("runs")} /> : null}
      </box>
      {error ? (
        <text fg={C.err}>{unreachable ? `API unreachable at ${oneLine(api.baseUrl, 60)} - press ctrl+r to retry` : errorText(error)}</text>
      ) : <text> </text>}
      <text fg={C.dim}>{HINTS[screen]}</text>
    </box>
  )
}
