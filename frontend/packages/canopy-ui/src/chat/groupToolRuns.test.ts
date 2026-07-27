import { describe, expect, it } from "vitest"

import { MIN_RUN_TO_GROUP, groupToolRuns, runHasError, summariseRun } from "./groupToolRuns"
import type { ChatRow } from "./pairToolMessages"
import type { Message } from "./protocol"

function msg(over: Partial<Message> = {}): Message {
  return {
    id: "m", turn_index: 0, role: "tool_use", content: {}, plaintext: "",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "", ...over,
  }
}

const pair = (i: number, name = "Bash", result?: Partial<Message>): ChatRow => ({
  kind: "tool_pair",
  use: msg({ id: `u${i}`, content: { id: `t${i}`, name } }),
  result: result ? msg({ id: `r${i}`, role: "tool_result", ...result }) : msg({ id: `r${i}`, role: "tool_result" }),
  key: `pair-${i}`,
})

const prose = (i: number): ChatRow => ({
  kind: "message",
  message: msg({ id: `p${i}`, role: "assistant", plaintext: "words" }),
  key: `msg-${i}`,
})

describe("groupToolRuns", () => {
  it("collapses a run of consecutive tool calls into one row", () => {
    const out = groupToolRuns([pair(1), pair(2), pair(3), pair(4)])
    expect(out).toHaveLength(1)
    expect(out[0].kind).toBe("tool_run")
  })

  it("prose breaks a run, so the conversation stays legible", () => {
    // The whole point: an agent's own words must never be buried inside a
    // collapsed group of the calls that surrounded them.
    const out = groupToolRuns([pair(1), pair(2), pair(3), prose(1), pair(4), pair(5), pair(6)])
    expect(out.map((r) => r.kind)).toEqual(["tool_run", "message", "tool_run"])
  })

  it("leaves short runs alone", () => {
    // Wrapping one or two calls costs a click and hides nothing.
    const out = groupToolRuns([pair(1), pair(2)])
    expect(out.map((r) => r.kind)).toEqual(["tool_pair", "tool_pair"])
    expect(MIN_RUN_TO_GROUP).toBe(3)
  })

  it("keeps every call, in order, inside the group", () => {
    const out = groupToolRuns([pair(1), pair(2), pair(3)])
    const run = out[0] as { rows: ChatRow[] }
    expect(run.rows.map((r) => r.key)).toEqual(["pair-1", "pair-2", "pair-3"])
  })

  it("summarises a run by count and the tools it used", () => {
    expect(summariseRun([pair(1, "Bash"), pair(2, "Read"), pair(3, "Bash")]))
      .toBe("3 tool calls · Bash, Read")
  })

  it("flags a run containing a failure", () => {
    // A collapsed group must not hide the one thing worth stopping for.
    expect(runHasError([pair(1), pair(2)])).toBe(false)
    expect(runHasError([pair(1), pair(2, "Bash", { status: "error" })])).toBe(true)
    expect(runHasError([pair(1, "Bash", { content: { is_error: true } })])).toBe(true)
  })

  it("a session of only prose is untouched", () => {
    const out = groupToolRuns([prose(1), prose(2)])
    expect(out.map((r) => r.kind)).toEqual(["message", "message"])
  })
})
