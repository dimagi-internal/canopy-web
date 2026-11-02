import { describe, expect, it } from "vitest"

import type { Message, SessionState, WsEvent } from "./protocol"
import { markSendDelivered, sessionReducer, withPendingSend } from "./sessionReducer"

/**
 * Your own line, from the moment you press send to the transcript's echo of it:
 * the open page must end where a reload does — one row, in its place.
 */

const ME = { name: "Jon", user_id: 7 }

function state(over: Partial<SessionState> = {}): SessionState {
  return { messages: [], active_draft: null, participants: [], presence_user_ids: [], current_user_id: 7, ...over }
}

function row(turn: number, over: Partial<Message> = {}): Message {
  return {
    id: `r${turn}`, turn_index: turn, role: "assistant", content: {}, plaintext: `row ${turn}`,
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-11-02T10:00:00Z", ...over,
  }
}

const said = (turn: number, text: string, over: Partial<Message> = {}) =>
  row(turn, { role: "user", plaintext: text, content: { text }, author: ME, ...over })

const echo = (data: { message_id: string; turn_index: number; plaintext: string; client_id?: string }): WsEvent =>
  ({ event: "chat.user_message", data: { author: ME, ...data } }) as WsEvent

const snapshot = (data: Partial<SessionState>): WsEvent => ({ event: "session.state", data: state(data) }) as WsEvent

/** Pressed send: the optimistic row, then the server's receipt. */
function sent(prev: SessionState, clientId: string, text: string): SessionState {
  return markSendDelivered(withPendingSend(prev, { clientId, text }), clientId, `transient:${clientId}`)
}

const texts = (s: SessionState) => s.messages.map((m) => m.plaintext)

describe("the echo of your line", () => {
  it("is matched by the send's nonce, whatever the words look like by then", () => {
    let s = sent(state({ messages: [row(64)] }), "c1", "see attached")
    s = sessionReducer(s, echo({ message_id: "u1", turn_index: 128, plaintext: "see attached\n[attached: a.png]", client_id: "c1" }))
    expect(s.messages.map((m) => m.id)).toEqual(["r64", "u1"])
    expect(s.messages[1].status).toBe("complete")
  })

  it("never takes over an older line with the same words", () => {
    // You said "yes" earlier, and say it again.
    let s = sent(state({ messages: [said(64, "yes", { content: { text: "yes", client_id: "c0" } }), row(128)] }), "c1", "yes")
    s = sessionReducer(s, echo({ message_id: "u2", turn_index: 192, plaintext: "yes", client_id: "c1" }))
    expect(s.messages.map((m) => m.id)).toEqual(["r64", "r128", "u2"])
  })

  it("without a nonce, matches only a line still waiting for its echo", () => {
    let s = state({ messages: [said(64, "yes"), row(128)] })
    s = sessionReducer(s, echo({ message_id: "u2", turn_index: 192, plaintext: "yes" }))
    expect(s.messages.map((m) => m.id)).toEqual(["r64", "r128", "u2"])
  })

  it("is someone else's line when its nonce is not one this page holds", () => {
    let s = sent(state({ messages: [row(64)] }), "c1", "yes")
    s = sessionReducer(s, echo({ message_id: "u9", turn_index: 128, plaintext: "yes", client_id: "c-other" }))
    expect(s.messages).toHaveLength(3)
    expect(s.messages.find((m) => m.content?.client_id === "c1")?.id).toBe("transient:c1")
  })

  it("lands where its ordinal puts it, above rows the turn before it shipped late", () => {
    // Sent while the previous turn was still running: its last rows arrive after the send.
    let s = sent(state({ messages: [row(64)] }), "c1", "next")
    s = sessionReducer(s, { event: "chat.stream_start", data: { message_id: "late", turn_index: 100 } } as WsEvent)
    s = sessionReducer(s, echo({ message_id: "u1", turn_index: 128, plaintext: "next", client_id: "c1" }))
    s = sessionReducer(s, { event: "chat.stream_start", data: { message_id: "reply", turn_index: 192 } } as WsEvent)
    expect(s.messages.map((m) => m.id)).toEqual(["r64", "late", "u1", "reply"])
  })
})

describe("a reconnect while your line is on its way", () => {
  const delivering = (clientId: string) =>
    [{ turn_id: "t1", client_id: clientId, author: null, text: "", sent_at: "", state: "delivering" as const }]

  it("keeps it while the server still lists it, its turn over or not", () => {
    const s = sent(state({ messages: [row(64)] }), "c1", "are we done?")
    const next = sessionReducer(s, snapshot({ messages: [row(64), row(128)], queued: delivering("c1") }))
    expect(texts(next)).toContain("are we done?")
  })

  it("lets the snapshot's copy win once it has landed", () => {
    const s = sent(state({ messages: [row(64)] }), "c1", "are we done?")
    const landed = said(128, "are we done?", { id: "u1", content: { text: "are we done?", client_id: "c1" } })
    const next = sessionReducer(s, snapshot({ messages: [row(64), landed], queued: [] }))
    expect(next.messages.map((m) => m.id)).toEqual(["r64", "u1"])
  })

  it("keeps it even when an older line said the same words", () => {
    const old = said(64, "yes", { content: { text: "yes", client_id: "c0" } })
    const s = sent(state({ messages: [old] }), "c1", "yes")
    const next = sessionReducer(s, snapshot({ messages: [old, row(128)], queued: delivering("c1") }))
    expect(next.messages.filter((m) => m.plaintext === "yes")).toHaveLength(2)
  })
})
