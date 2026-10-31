import { describe, expect, it } from "vitest"

import type { Draft, Message, SessionState, WsEvent } from "./protocol"
import { markSendDelivered, markSendFailed, sessionReducer, withPendingSend } from "./sessionReducer"

const draft: Draft = { id: "d1", slot: "next", status: "open", body: "", version: 0, last_editor: 0, last_edit_at: "" }

function state(over: Partial<SessionState> = {}): SessionState {
  return { messages: [], active_draft: null, participants: [], presence_user_ids: [], current_user_id: 7, ...over }
}

function row(turn: number, over: Partial<Message> = {}): Message {
  return {
    id: `r${turn}`, turn_index: turn, role: "assistant", content: {}, plaintext: `row ${turn}`,
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-31T10:00:00Z", ...over,
  }
}

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => row(from + i))
const snapshot = (data: Partial<SessionState>): WsEvent => ({ event: "session.state", data: state(data) }) as WsEvent

describe("a reconnect's snapshot keeps the history this tab loaded", () => {
  it("keeps rows below the snapshot's oldest and merges in order", () => {
    // Loaded everything (0..20) and the server says so.
    const prev = state({ messages: range(0, 20), has_more_before: false })
    const next = sessionReducer(prev, snapshot({ messages: range(11, 22), has_more_before: true }))
    expect(next.messages.map((m) => m.turn_index)).toEqual(range(0, 22).map((m) => m.turn_index))
    // The kept older rows reach the beginning, so nothing more is before them.
    expect(next.has_more_before).toBe(false)
  })

  it("reads the snapshot's oldest_loaded_turn_index when the server sends it", () => {
    const prev = state({ messages: range(0, 20) })
    const next = sessionReducer(prev, snapshot({ messages: range(11, 22), oldest_loaded_turn_index: 15 }))
    // Rows 11..14 come from the snapshot, 0..10 from what was loaded — none twice.
    expect(next.messages.map((m) => m.turn_index)).toEqual(range(0, 22).map((m) => m.turn_index))
  })

  it("drops them when the tab's rows do not reach the snapshot (a hole), and says more is before", () => {
    const prev = state({ messages: range(0, 20), has_more_before: false })
    const next = sessionReducer(prev, snapshot({ messages: range(200, 210) }))
    expect(next.messages.map((m) => m.turn_index)).toEqual(range(200, 210).map((m) => m.turn_index))
    expect(next.has_more_before).toBe(true)
  })

  it("takes the snapshot's word when nothing older was loaded", () => {
    const next = sessionReducer(state({ messages: range(11, 20) }), snapshot({ messages: range(11, 22), has_more_before: true }))
    expect(next.messages).toHaveLength(12)
    expect(next.has_more_before).toBe(true)
  })

  it("an empty snapshot replaces everything", () => {
    const next = sessionReducer(state({ messages: range(0, 5) }), snapshot({ messages: [] }))
    expect(next.messages).toEqual([])
  })
})

describe("draft.committed in another tab of the same author", () => {
  it("only clears the draft — no row from that tab's own box", () => {
    const prev = state({ active_draft: { ...draft, body: "something else entirely" }, messages: [row(1)] })
    const next = sessionReducer(prev, {
      event: "draft.committed",
      data: { user_message_id: "100", draft_id: "d1", client_id: "cid-from-the-other-tab" },
    } as WsEvent)
    expect(next.messages).toHaveLength(1)
    expect(next.active_draft?.body).toBe("")
  })

  it("so the transcript's echo lands once", () => {
    let s = state({ active_draft: { ...draft, body: "" }, messages: [row(1)] })
    s = sessionReducer(s, {
      event: "draft.committed",
      data: { user_message_id: "100", draft_id: "d1", client_id: "c9" },
    } as WsEvent)
    s = sessionReducer(s, {
      event: "chat.user_message",
      data: { message_id: "100", turn_index: 2, plaintext: "hello", client_id: "c9", author: { name: "Me", user_id: 7 } },
    } as WsEvent)
    expect(s.messages.filter((m) => m.role === "user")).toHaveLength(1)
    expect(s.messages[1].plaintext).toBe("hello")
  })

  it("the sending tab confirms its own pending row", () => {
    let s = withPendingSend(state({ active_draft: draft }), { clientId: "c1", text: "hi" })
    s = sessionReducer(s, {
      event: "draft.committed",
      data: { user_message_id: "55", draft_id: "d1", client_id: "c1" },
    } as WsEvent)
    expect(s.messages).toHaveLength(1)
    expect(s.messages[0]).toMatchObject({ id: "55", status: "complete" })
  })
})

describe("one optimistic-send path", () => {
  it("withPendingSend adds a send once, however often it is reported", () => {
    let s = withPendingSend(state(), { clientId: "c1", text: "hi" })
    s = withPendingSend(s, { clientId: "c1", text: "hi" })
    expect(s.messages).toHaveLength(1)
    expect(s.messages[0].status).toBe("pending")
  })

  it("a failed send keeps its words, marked; a late failure never overturns a delivery", () => {
    const pending = withPendingSend(state(), { clientId: "c1", text: "hi" })
    const failed = markSendFailed(pending, "c1", "Not sent: boom")
    expect(failed.messages[0]).toMatchObject({ status: "error", error_detail: "Not sent: boom", plaintext: "hi" })
    const delivered = markSendDelivered(pending, "c1")
    expect(markSendFailed(delivered, "c1", "late")).toBe(delivered)
  })
})
