import { describe, expect, test } from "bun:test"
import { oneLine, pad, safe } from "../src/text.ts"

describe("safe()", () => {
  test("removes whole CSI, OSC and other escape sequences", () => {
    expect(safe("\x1b[31mred\x1b[0m")).toBe("red")
    expect(safe("\x1b[2Jx\x1b[1;2H")).toBe("x")
    expect(safe("\x1b]0;title\x07after")).toBe("after")
    expect(safe("\x1b]8;;http://x\x1b\\link")).toBe("link")
    expect(safe("a\x1bcb\x1b(Bc")).toBe("abc")
  })

  test("removes 8-bit C1 CSI and OSC sequences", () => {
    expect(safe("\u009b31mred\u009b0m")).toBe("red")
    expect(safe("\u009d0;title\u0007ok")).toBe("ok")
  })

  test("removes carriage return, NUL, other C0 controls, DEL and C1 controls", () => {
    expect(safe("line\roverwrite")).toBe("lineoverwrite")
    expect(safe("a\u0000b\u0007c\u007fd\u0085e")).toBe("abcde")
    expect(safe("x\u0008y\u000Bz\u001Fw")).toBe("xyzw")
  })

  test("keeps newline and tab", () => {
    expect(safe("a\nb\tc")).toBe("a\nb\tc")
  })

  test("strips bidi overrides, isolates and marks", () => {
    expect(safe("\u202eevil")).toBe("evil")
    expect(safe("a\u2066b\u2069")).toBe("ab")
    expect(safe("a\u200eb\u200fc\u061cd\u202a\u202b\u202c\u202d\u2067\u2068")).toBe("abcd")
  })

  test("null and undefined become empty strings", () => {
    expect(safe(null)).toBe("")
    expect(safe(undefined)).toBe("")
  })
})

describe("oneLine() and pad()", () => {
  test("collapses newlines and tabs into single spaces", () => {
    expect(oneLine("a\nb\tc\n\nd")).toBe("a b c d")
  })

  test("truncates to max with an ellipsis", () => {
    const out = oneLine("x".repeat(10), 5)
    expect(out).toBe("xxxx…")
    expect(out.length).toBe(5)
  })

  test("pad() pads short strings and truncates long ones", () => {
    expect(pad("a", 3)).toBe("a  ")
    expect(pad("abcdef", 3)).toBe("abc")
  })
})
