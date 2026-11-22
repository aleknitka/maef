/** Untrusted text handling: strip control characters (except newline and tab) before anything is rendered. */
// Whole terminal sequences first (so no printable residue like "[31m" is left behind), then stray controls.
const SEQUENCES = new RegExp(
  [
    "(?:\\u001B\\[|\\u009B)[\\u0020-\\u003F]*[\\u0040-\\u007E]", // CSI (7-bit and 8-bit introducer)
    "(?:\\u001B\\]|\\u009D)[^\\u0007\\u001B\\u009C]*(?:\\u0007|\\u001B\\\\|\\u009C|$)", // OSC, ended by BEL, ST or end of input
    "\\u001B[\\u0020-\\u002F]*[\\u0030-\\u007E]", // other ESC sequences (two-byte and nF)
  ].join("|"),
  "g",
)
// C0 except \t and \n (this includes \r), DEL, C1, and bidi controls.
const CONTROL = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g

export function safe(value: unknown): string {
  return String(value ?? "").replace(SEQUENCES, "").replace(CONTROL, "")
}

/** One-line variant: also collapses newlines and tabs. */
export function oneLine(value: unknown, max = 120): string {
  const s = safe(value).replace(/[\n\t]+/g, " ")
  return s.length > max ? s.slice(0, max - 1) + "…" : s
}

/** Compact `key=value` summary of an event payload. */
export function summarize(payload: unknown, max = 100): string {
  if (payload === null || typeof payload !== "object") return oneLine(payload, max)
  const parts: string[] = []
  for (const [k, v] of Object.entries(payload as Record<string, unknown>)) {
    const text = v !== null && typeof v === "object" ? JSON.stringify(v) : String(v)
    parts.push(`${k}=${text}`)
  }
  return oneLine(parts.join(" "), max)
}

export function pad(s: string, n: number): string {
  return s.length >= n ? s.slice(0, n) : s + " ".repeat(n - s.length)
}
