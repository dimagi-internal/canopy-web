import { describe, expect, it } from "vitest"

import type { Message } from "./protocol"
import { tailDivergence } from "./tailCheck"

function row(turn: number, over: Partial<Message> = {}): Message {
  return {
    id: `r${turn}`, turn_index: turn, role: "assistant", content: {}, plaintext: `row ${turn}`,
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "", ...over,
  }
}

describe("tailDivergence", () => {
  it("is null when the page holds what the server does", () => {
    expect(tailDivergence([row(1), row(64), row(128)], [row(64), row(128)])).toBeNull()
  })

  it("names a server row the page lost — your line", () => {
    const yours = row(64, { id: "u1", role: "user" })
    expect(tailDivergence([row(1), row(128)], [yours, row(128)])).toEqual({ missing: ["u1"], extra: [], reordered: false })
  })

  it("names a row the page holds that the server does not", () => {
    expect(tailDivergence([row(64), row(100), row(128)], [row(64), row(128)])?.extra).toEqual(["r100"])
  })

  it("notices the same rows in a different order", () => {
    expect(tailDivergence([row(128), row(64)], [row(64), row(128)])?.reordered).toBe(true)
  })

  it("ignores your lines still on their way, and rows older than the tail", () => {
    const pending = row(200, { id: "local:c1", role: "user", status: "pending" })
    expect(tailDivergence([row(1), row(64), pending], [row(64)])).toBeNull()
  })
})
