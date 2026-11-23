import { describe, expect, it } from "vitest"

import type { Message, SessionState, WsEvent } from "./protocol"
import { sessionReducer } from "./sessionReducer"

function state(messages: Message[] = []): SessionState {
  return { messages, active_draft: null, participants: [], presence_user_ids: [], current_user_id: 0 }
}

function msg(id: string, role: Message["role"], plaintext: string, turn_index = 1): Message {
  return {
    id, turn_index, role, content: {}, plaintext, status: "complete", error_detail: null,
    started_at: null, completed_at: null, created_at: new Date().toISOString(),
  }
}

const live = (text: string, n = 0): WsEvent =>
  ({ event: "chat.live_text", data: { message_id: `live:t1:${n}`, turn_id: "t1", text } }) as WsEvent

describe("chat.live_text: the reply being written", () => {
  it("draws the reply whole as it grows, under its own id", () => {
    let s = sessionReducer(state([msg("u1", "user", "Which partner?")]), live("EHA is"))
    s = sessionReducer(s, live("EHA is furthest behind"))
    const drawn = s.messages.filter((m) => m.id === "live:t1:0")
    expect(drawn).toHaveLength(1)
    expect(drawn[0].plaintext).toBe("EHA is furthest behind")
    expect(drawn[0].status).toBe("streaming")
  })

  it("gives way to the stored reply when it arrives, without doubling", () => {
    const text = "EHA is furthest behind, but only just: 59.7% of its babies show healthy growth."
    let s = sessionReducer(state([msg("u1", "user", "Which partner?")]), live(text.slice(0, 40)))
    s = sessionReducer(s, { event: "chat.stream_start", data: { message_id: "m9", turn_index: 5 } })
    s = sessionReducer(s, { event: "chat.stream_complete", data: { message_id: "m9", plaintext: text } })
    expect(s.messages.map((m) => m.id)).toEqual(["u1", "m9"])
    // A late live frame for the same reply does not bring it back.
    s = sessionReducer(s, live(text))
    expect(s.messages.map((m) => m.id)).toEqual(["u1", "m9"])
  })

  it("is not taken for an earlier answer that began the same way", () => {
    const old = "EHA is furthest behind, last week, at 50%."
    let s = state([msg("a0", "assistant", old), msg("u1", "user", "And now?")])
    s = sessionReducer(s, live("EHA is furthest behind,"))
    expect(s.messages.map((m) => m.id)).toEqual(["a0", "u1", "live:t1:0"])
  })

  it("keeps each reply of a turn apart", () => {
    let s = sessionReducer(state([msg("u1", "user", "Go")]), live("Reading the run indicators now."))
    s = sessionReducer(s, live("Done: EHA.", 1))
    expect(s.messages.filter((m) => m.id.startsWith("live:")).map((m) => m.plaintext))
      .toEqual(["Reading the run indicators now.", "Done: EHA."])
  })
})
