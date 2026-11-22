import { useMemo, useState } from "react"
import type { Api, RunRecord } from "./api.ts"
import { oneLine, pad } from "./text.ts"
import { type ScreenProps, usePoll, C } from "./ui.tsx"

const row = (r: RunRecord) =>
  `${pad(oneLine(r.id.slice(0, 8)), 9)}${pad(oneLine(r.agent_id, 22), 23)}${pad(oneLine(r.status, 10), 11)}` +
  `${pad(`${r.tokens_used ?? "-"}/${r.reserved_tokens}`, 15)}${oneLine(r.created_at.slice(0, 19).replace("T", " "), 19)}`

export function Runs({ api, onOpen, onError, clearError, pollMs, retry, active }: ScreenProps & { api: Api; onOpen: (id: string) => void; active: boolean }) {
  const [runs, setRuns] = useState<RunRecord[] | null>(null)
  const [sig, setSig] = useState("")
  usePoll(async (alive) => {
    try {
      const list = await api.runs()
      if (!alive()) return
      const next = JSON.stringify(list)
      setSig((prev) => { if (prev !== next) setRuns(list); return next })
      clearError()
    } catch (e) { if (alive()) onError(e) }
  }, pollMs, [retry])

  const options = useMemo(() => (runs ?? []).map((r) => ({ name: row(r), description: "", value: r.id })), [runs])
  if (!runs) return <text fg={C.dim}>Loading runs…</text>
  return (
    <box flexDirection="column" flexGrow={1}>
      <text fg={C.dim}>{`${pad("ID", 9)}${pad("AGENT", 23)}${pad("STATUS", 11)}${pad("TOKENS", 15)}CREATED`}</text>
      {runs.length === 0 ? <text fg={C.dim}>No runs yet.</text> : (
        <select
          flexGrow={1}
          options={options}
          focused={active}
          showDescription={false}
          onSelect={(i) => { const r = runs[i]; if (r) onOpen(r.id) }}
        />
      )}
    </box>
  )
}
