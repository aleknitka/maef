import { useEffect, useRef } from "react"
import { ApiError } from "./api.ts"

export const C = { accent: "#58A6FF", dim: "#8B949E", err: "#FF7B72", ok: "#3FB950", warn: "#D29922", fg: "#E6EDF3" }

export function statusColor(status: string): string {
  if (status === "succeeded") return C.ok
  if (status === "failed") return C.err
  if (status === "cancelled") return C.warn
  return C.accent
}

/** Message shown on the error line: the API's own message/code only, never a stack trace. */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display()
  return "Unexpected error."
}

/** Run `fn` now, whenever `deps` change, and every `ms` (if given). Ignores results after unmount. */
export function usePoll(fn: (alive: () => boolean) => Promise<void> | void, ms: number | null, deps: unknown[]) {
  const latest = useRef(fn)
  latest.current = fn
  useEffect(() => {
    let live = true
    const alive = () => live
    const tick = () => { void Promise.resolve(latest.current(alive)).catch(() => {}) }
    tick()
    const timer = ms ? setInterval(tick, ms) : null
    return () => { live = false; if (timer) clearInterval(timer) }
  }, deps)
}

export interface ScreenProps {
  onError: (e: unknown) => void
  clearError: () => void
  typing: { current: boolean }
  pollMs: number
  retry: number
}
