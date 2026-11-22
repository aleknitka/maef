import { useRef, useState } from "react"
import { useKeyboard } from "@opentui/react"
import type { TextareaRenderable } from "@opentui/core"
import type { Agent, Api, BudgetOverride, RunPreview } from "./api.ts"
import { oneLine } from "./text.ts"
import { type ScreenProps, C } from "./ui.tsx"

const FIELDS = ["instruction", "max_tokens", "max_turns", "max_duration_seconds"] as const
const LABELS = ["Instruction", "Max tokens", "Max turns", "Max duration (s)"]

export function NewRun({ api, agent, onSubmitted, onError, clearError, typing, active }: ScreenProps & { api: Api; agent: Agent | null; onSubmitted: (id: string) => void; active: boolean }) {
  const area = useRef<TextareaRenderable>(null)
  const [focus, setFocus] = useState(0)
  const [nums, setNums] = useState<Record<string, string>>({})
  const numsRef = useRef<Record<string, string>>({})
  const [preview, setPreview] = useState<RunPreview | null>(null)
  const [busy, setBusy] = useState(false)
  typing.current = active

  const [problem, setProblem] = useState("")
  const gather = () => {
    const instruction = area.current?.plainText ?? ""
    if (!instruction.trim()) { setProblem("Instruction is required."); return null }
    const budget: BudgetOverride = {}
    for (const f of FIELDS.slice(1)) {
      const raw = (numsRef.current[f] ?? "").trim()
      if (!raw) continue
      if (!/^\d+$/.test(raw) || Number(raw) < 1) { setProblem(`${f} must be a positive whole number.`); return null }
      budget[f as keyof BudgetOverride] = Number(raw)
    }
    setProblem("")
    return { instruction, budget }
  }

  async function run(kind: "preview" | "submit") {
    if (!agent || busy) return
    const g = gather()
    if (!g) return
    setBusy(true)
    try {
      if (kind === "preview") { setPreview(await api.preview(agent.id, g.instruction, g.budget)); clearError() }
      else { const r = await api.submit(agent.id, g.instruction, g.budget); clearError(); onSubmitted(r.run_id) }
    } catch (e) { onError(e) } finally { setBusy(false) }
  }

  useKeyboard((key) => {
    if (!active) return
    if (key.ctrl && key.name === "s") void run("submit")
    else if (key.ctrl && key.name === "p") void run("preview")
    else if (key.name === "tab" && focus !== 0) setFocus((f) => (f + (key.shift ? FIELDS.length - 1 : 1)) % FIELDS.length)
  })

  if (!agent) return <text fg={C.dim}>No agent selected. Press 1 and choose an agent with Enter.</text>
  return (
    <box flexDirection="column" flexGrow={1}>
      <text fg={C.accent}>{`Agent: ${oneLine(agent.id)}  ${oneLine(agent.display_name)}`}</text>
      <box title={`${LABELS[0]}${focus === 0 ? " *" : ""}`} border height={7} borderColor={focus === 0 ? C.accent : C.dim}>
        <textarea ref={area} onKeyDown={(e) => { if (e.name === "tab") { e.preventDefault(); setFocus(e.shift ? FIELDS.length - 1 : 1) } }} focused={active && focus === 0} placeholder="What should the agent do?" flexGrow={1} />
      </box>
      <box flexDirection="row">
        {FIELDS.slice(1).map((f, i) => (
          <box key={f} title={`${LABELS[i + 1]}${focus === i + 1 ? " *" : ""}`} border width={24} height={3} borderColor={focus === i + 1 ? C.accent : C.dim}>
            <input
              focused={active && focus === i + 1}
              placeholder="agent default"
              value={nums[f] ?? ""}
              onInput={(v: string) => { numsRef.current = { ...numsRef.current, [f]: v }; setNums(numsRef.current) }}
            />
          </box>
        ))}
      </box>
      {problem ? <text fg={C.err}>{problem}</text> : null}
      {busy ? <text fg={C.dim}>Working…</text> : null}
      {preview ? (
        <box flexDirection="column">
          <text fg={C.ok}>Preview (nothing submitted)</text>
          <text>{`budget: tokens=${preview.effective_budget.max_tokens} turns=${preview.effective_budget.max_turns} duration=${preview.effective_budget.max_duration_seconds}s`}</text>
          <text>{`model profile: ${oneLine(preview.model_profile)}  sandbox: ${oneLine(preview.sandbox_profile)}`}</text>
          <text>{`skills: ${oneLine(preview.skills.join(", ") || "-")}  mcp: ${oneLine(preview.mcp_servers.join(", ") || "-")}`}</text>
        </box>
      ) : null}
    </box>
  )
}
