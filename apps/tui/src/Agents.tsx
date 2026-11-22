import { useMemo, useState } from "react"
import type { Agent, Api } from "./api.ts"
import { oneLine, pad, safe } from "./text.ts"
import { type ScreenProps, usePoll, C } from "./ui.tsx"

export function Agents({ api, onPick, onError, clearError, retry, active }: ScreenProps & { api: Api; onPick: (a: Agent) => void; active: boolean }) {
  const [agents, setAgents] = useState<Agent[] | null>(null)
  usePoll(async (alive) => {
    try {
      const list = await api.agents()
      if (!alive()) return
      setAgents(list)
      clearError()
    } catch (e) { if (alive()) onError(e) }
  }, null, [retry])

  const options = useMemo(
    () => (agents ?? []).map((a) => ({ name: `${pad(oneLine(a.id, 28), 28)} ${oneLine(a.display_name, 40)}`, description: "  " + oneLine(a.description, 160), value: a })),
    [agents],
  )
  if (!agents) return <text fg={C.dim}>Loading agents…</text>
  if (!agents.length) return <text fg={C.dim}>No agents defined.</text>
  return (
    <select
      flexGrow={1}
      options={options}
      focused={active}
      showScrollIndicator
      onSelect={(i) => { const a = agents[i]; if (a) onPick(a) }}
    />
  )
}
