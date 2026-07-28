import { describe, expect, it } from "vitest"

import type { Draft, Message, SessionState, WsEvent } from "./protocol"
import { sessionReducer } from "./sessionReducer"

const baseDraft: Draft = {
  id: "d1",
  slot: "next",
  status: "open",
  body: "",
  version: 0,
  last_editor: 0,
  last_edit_at: "",
}

function makeState(overrides: Partial<SessionState> = {}): SessionState {
  return {
    messages: [],
    active_draft: null,
    participants: [],
    presence_user_ids: [],
    current_user_id: 0,
    ...overrides,
  }
}

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: "1",
    turn_index: 1,
    role: "assistant",
    content: {},
    plaintext: "",
    status: "pending",
    error_detail: null,
    started_at: null,
    completed_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

describe("sessionReducer — chat stream", () => {
  it("session.state replaces the whole state", () => {
    const prev = makeState({ messages: [makeMessage()] })
    const replacement = makeState({ current_user_id: 42 })
    const next = sessionReducer(prev, {
      event: "session.state",
      data: replacement,
    } as WsEvent)
    expect(next).toBe(replacement)
  })

  it("chat.stream_start flips a matching message to streaming", () => {
    const m = makeMessage({ id: "7", status: "pending" })
    const prev = makeState({ messages: [m] })
    const next = sessionReducer(prev, {
      event: "chat.stream_start",
      data: { message_id: "7", turn_index: 3 },
    } as WsEvent)
    expect(next.messages).toHaveLength(1)
    expect(next.messages[0].status).toBe("streaming")
  })

  it("chat.stream_start for an unknown id CREATES the assistant message (upsert)", () => {
    const m = makeMessage({ id: "7", role: "user", status: "complete" })
    const prev = makeState({ messages: [m] })
    const next = sessionReducer(prev, {
      event: "chat.stream_start",
      data: { message_id: "99", turn_index: 5 },
    } as WsEvent)
    expect(next.messages).toHaveLength(2)
    expect(next.messages[1]).toMatchObject({
      id: "99",
      role: "assistant",
      status: "streaming",
      plaintext: "",
      turn_index: 5,
    })
  })

  it("chat.delta appends text to plaintext", () => {
    const m = makeMessage({ id: "7", plaintext: "Hello" })
    const prev = makeState({ messages: [m] })
    const next = sessionReducer(prev, {
      event: "chat.delta",
      data: { message_id: "7", text: " world" },
    } as WsEvent)
    expect(next.messages[0].plaintext).toBe("Hello world")
  })

  it("chat.stream_complete replaces plaintext and marks complete", () => {
    const m = makeMessage({ id: "7", plaintext: "stale partial", status: "streaming" })
    const prev = makeState({ messages: [m] })
    const next = sessionReducer(prev, {
      event: "chat.stream_complete",
      data: { message_id: "7", plaintext: "final answer" },
    } as WsEvent)
    expect(next.messages[0].plaintext).toBe("final answer")
    expect(next.messages[0].status).toBe("complete")
  })

  it("chat.stream_error sets error_detail and status=error", () => {
    const m = makeMessage({ id: "7", status: "streaming" })
    const prev = makeState({ messages: [m] })
    const next = sessionReducer(prev, {
      event: "chat.stream_error",
      data: { message_id: "7", detail: "cancelled" },
    } as WsEvent)
    expect(next.messages[0].status).toBe("error")
    expect(next.messages[0].error_detail).toBe("cancelled")
  })

  it("chat.stream_cancelled stamps a partial-length detail", () => {
    const m = makeMessage({ id: "7", status: "streaming" })
    const prev = makeState({ messages: [m] })
    const next = sessionReducer(prev, {
      event: "chat.stream_cancelled",
      data: { message_id: "7", partial_len: 142 },
    } as WsEvent)
    expect(next.messages[0].status).toBe("error")
    expect(next.messages[0].error_detail).toMatch(/142/)
  })

  it("chat.tool_use appends a tool row carrying the block as its content", () => {
    // The block IS the content the UI pairs and renders on — dropping the
    // frame (the old no-op) meant a running agent's tool calls only appeared
    // after a manual reload, which is precisely when you want to see them.
    const prev = makeState({ messages: [makeMessage()] })
    const next = sessionReducer(prev, {
      event: "chat.tool_use",
      data: {
        parent_message_id: null,
        tool_message_id: "seq:64",
        turn_index: 64,
        block: { id: "toolu_1", name: "Bash", input: { command: "ls" }, text: "" },
      },
    } as WsEvent)
    expect(next.messages).toHaveLength(2)
    const row = next.messages[1]
    expect(row.role).toBe("tool_use")
    expect(row.id).toBe("seq:64")
    expect(row.turn_index).toBe(64)
    expect(row.content).toEqual({
      id: "toolu_1",
      name: "Bash",
      input: { command: "ls" },
      text: "",
    })
  })

  it("chat.tool_result carries the result body as plaintext", () => {
    const prev = makeState({ messages: [] })
    const next = sessionReducer(prev, {
      event: "chat.tool_result",
      data: {
        parent_message_id: null,
        tool_message_id: "seq:128",
        turn_index: 128,
        block: { tool_use_id: "toolu_1", is_error: false, text: "a.txt" },
      },
    } as WsEvent)
    expect(next.messages[0].role).toBe("tool_result")
    expect(next.messages[0].plaintext).toBe("a.txt")
    expect(next.messages[0].status).toBe("complete")
  })

  it("a failed tool result is marked error so the pair renders as one", () => {
    const next = sessionReducer(makeState(), {
      event: "chat.tool_result",
      data: {
        parent_message_id: null,
        tool_message_id: "seq:128",
        turn_index: 128,
        block: { tool_use_id: "toolu_1", is_error: true, text: "boom" },
      },
    } as WsEvent)
    expect(next.messages[0].status).toBe("error")
  })

  it("a re-delivered tool frame upserts instead of doubling the row", () => {
    // Reconnect catch-up and a retried post both re-ship rows; a duplicated
    // tool_use would leave one copy permanently stuck showing "running…".
    const frame = {
      event: "chat.tool_use",
      data: {
        parent_message_id: null,
        tool_message_id: "seq:64",
        turn_index: 64,
        block: { id: "toolu_1", name: "Bash", input: {}, text: "" },
      },
    } as WsEvent
    const once = sessionReducer(makeState(), frame)
    const twice = sessionReducer(once, frame)
    expect(twice.messages).toHaveLength(1)
  })

  it("a tool frame without an ordinal still lands after the newest row", () => {
    const prev = makeState({ messages: [makeMessage({ turn_index: 7 })] })
    const next = sessionReducer(prev, {
      event: "chat.tool_use",
      data: { parent_message_id: null, tool_message_id: "t1", block: {} },
    } as WsEvent)
    expect(next.messages[1].turn_index).toBe(8)
  })
})

describe("sessionReducer — drafts", () => {
  it("draft.updated keeps local body when echo's last_editor matches current_user_id", () => {
    // Echo-suppression: server echo arrives stale relative to the user's
    // own keystrokes; reducer must keep the local body and only accept
    // metadata. This is the most subtle branch in the file.
    const prev = makeState({
      current_user_id: 5,
      active_draft: { ...baseDraft, body: "local typing", version: 3 },
    })
    const next = sessionReducer(prev, {
      event: "draft.updated",
      data: {
        ...baseDraft,
        body: "stale server echo",
        last_editor: 5,
        version: 3,
      } as Draft,
    } as WsEvent)
    expect(next.active_draft?.body).toBe("local typing")
    expect(next.active_draft?.version).toBe(3)
  })

  it("draft.updated accepts the body when another user is editing", () => {
    const prev = makeState({
      current_user_id: 5,
      active_draft: { ...baseDraft, body: "old", last_editor: 7 },
    })
    const next = sessionReducer(prev, {
      event: "draft.updated",
      data: {
        ...baseDraft,
        body: "their text",
        last_editor: 7,
        version: 2,
      } as Draft,
    } as WsEvent)
    expect(next.active_draft?.body).toBe("their text")
  })

  it("draft.committed inserts the optimistic user message and clears the draft body", () => {
    // canopy adaptation: NO assistant placeholder here (draft.committed has
    // no assistant id) — only the user message is inserted; the assistant is
    // upserted later on chat.stream_start.
    const prev = makeState({
      active_draft: { ...baseDraft, body: "the prompt" },
      messages: [makeMessage({ id: "1", turn_index: 1 })],
    })
    const next = sessionReducer(prev, {
      event: "draft.committed",
      data: { user_message_id: "100", draft_id: "d1" },
    } as WsEvent)
    expect(next.messages).toHaveLength(2)
    expect(next.messages[1]).toMatchObject({
      id: "100",
      role: "user",
      plaintext: "the prompt",
      turn_index: 2,
    })
    // active_draft.body cleared so Enter doesn't re-send the same turn.
    expect(next.active_draft?.body).toBe("")
  })

  it("draft.committed then chat.stream_start makes the assistant reply visible", () => {
    // The load-bearing sequence: commit inserts the user msg, stream_start
    // upserts the assistant row, delta/complete fill it in.
    let s = makeState({ active_draft: { ...baseDraft, body: "hi" } })
    s = sessionReducer(s, {
      event: "draft.committed",
      data: { user_message_id: "u1", draft_id: "d1" },
    } as WsEvent)
    s = sessionReducer(s, {
      event: "chat.stream_start",
      data: { message_id: "a1", turn_index: 2 },
    } as WsEvent)
    s = sessionReducer(s, {
      event: "chat.stream_complete",
      data: { message_id: "a1", plaintext: "hello there" },
    } as WsEvent)
    expect(s.messages.map((m) => m.role)).toEqual(["user", "assistant"])
    expect(s.messages[1].plaintext).toBe("hello there")
    expect(s.messages[1].status).toBe("complete")
  })

  it("draft.discarded clears matching draft body", () => {
    const prev = makeState({
      active_draft: { ...baseDraft, id: "d1", body: "draft text" },
    })
    const next = sessionReducer(prev, {
      event: "draft.discarded",
      data: { draft_id: "d1" },
    } as WsEvent)
    expect(next.active_draft?.body).toBe("")
  })
})

describe("sessionReducer — presence", () => {
  it("presence.joined adds a user_id idempotently", () => {
    const prev = makeState({ presence_user_ids: [1, 2] })
    const next = sessionReducer(prev, {
      event: "presence.joined",
      data: { user_id: 3 },
    } as WsEvent)
    expect(next.presence_user_ids.sort()).toEqual([1, 2, 3])

    // Second join is a no-op (Set semantics).
    const after = sessionReducer(next, {
      event: "presence.joined",
      data: { user_id: 3 },
    } as WsEvent)
    expect(after.presence_user_ids.filter((id) => id === 3)).toHaveLength(1)
  })

  it("presence.left filters out the user_id", () => {
    const prev = makeState({ presence_user_ids: [1, 2, 3] })
    const next = sessionReducer(prev, {
      event: "presence.left",
      data: { user_id: 2 },
    } as WsEvent)
    expect(next.presence_user_ids).toEqual([1, 3])
  })
})

describe("sessionReducer — session.error draft_version_mismatch", () => {
  it("rolls active_draft back to the server's reported version + body", () => {
    const prev = makeState({
      active_draft: { ...baseDraft, version: 9, body: "stale local" },
    })
    const next = sessionReducer(prev, {
      event: "session.error",
      data: {
        message: "version mismatch",
        code: "draft_version_mismatch",
        detail: { current_version: 11, current_body: "server body" },
      },
    } as WsEvent)
    expect(next.active_draft?.version).toBe(11)
    expect(next.active_draft?.body).toBe("server body")
  })

  it("non-version-mismatch errors are no-ops to state (side-effect handled by hook)", () => {
    const prev = makeState({
      active_draft: { ...baseDraft, body: "x" },
    })
    const next = sessionReducer(prev, {
      event: "session.error",
      data: { message: "something else", code: "other" },
    } as WsEvent)
    expect(next).toBe(prev)
  })
})

describe("sessionReducer — live/durable reconciliation", () => {
  const liveToolUse = {
    event: "chat.tool_use",
    data: {
      parent_message_id: null,
      tool_message_id: "seq:-1",
      turn_index: -1,
      block: { id: "toolu_9", name: "Bash", input: { command: "ls" }, text: "" },
    },
  } as WsEvent

  const durableToolUse = {
    event: "chat.tool_use",
    data: {
      parent_message_id: null,
      tool_message_id: "42",
      turn_index: 128,
      block: { id: "toolu_9", name: "Bash", input: { command: "ls" }, text: "" },
    },
  } as WsEvent

  it("a durable row REPLACES the live placeholder for the same tool call", () => {
    // The same call arrives twice by design — live from a hook (no ordinal) and
    // durably from the transcript. They share only tool_use_id, so without
    // reconciling on it the user sees every tool call twice.
    const live = sessionReducer(makeState(), liveToolUse)
    expect(live.messages).toHaveLength(1)
    const settled = sessionReducer(live, durableToolUse)
    expect(settled.messages).toHaveLength(1)
  })

  it("the surviving row takes the durable identity, not the placeholder's", () => {
    // Otherwise later updates key on a `seq:-1` that no longer means anything.
    const settled = sessionReducer(
      sessionReducer(makeState(), liveToolUse),
      durableToolUse,
    )
    expect(settled.messages[0].id).toBe("42")
    expect(settled.messages[0].turn_index).toBe(128)
  })

  it("reconciles tool_result on tool_use_id too", () => {
    const mk = (id: string, turn_index: number) =>
      ({
        event: "chat.tool_result",
        data: {
          parent_message_id: null,
          tool_message_id: id,
          turn_index,
          block: { tool_use_id: "toolu_9", is_error: false, text: "a.txt" },
        },
      }) as WsEvent
    const settled = sessionReducer(
      sessionReducer(makeState(), mk("seq:-1", -1)),
      mk("77", 192),
    )
    expect(settled.messages).toHaveLength(1)
    expect(settled.messages[0].id).toBe("77")
  })

  it("two different tool calls stay two rows", () => {
    const other = {
      event: "chat.tool_use",
      data: {
        parent_message_id: null,
        tool_message_id: "seq:-1b",
        turn_index: -1,
        block: { id: "toolu_OTHER", name: "Read", input: {}, text: "" },
      },
    } as WsEvent
    const next = sessionReducer(sessionReducer(makeState(), liveToolUse), other)
    expect(next.messages).toHaveLength(2)
  })

  it("a row with no correlation id still falls back to matching on message id", () => {
    const noId = {
      event: "chat.tool_use",
      data: { parent_message_id: null, tool_message_id: "t1", block: {} },
    } as WsEvent
    const next = sessionReducer(sessionReducer(makeState(), noId), noId)
    expect(next.messages).toHaveLength(1)
  })
})

describe("sessionReducer — pending → complete lifecycle", () => {
  const mk = (status: string, id = "seq:-1") =>
    ({
      event: "chat.tool_use",
      data: {
        parent_message_id: null,
        tool_message_id: id,
        turn_index: -1,
        block: { id: "toolu_LIVE", name: "Bash", input: { command: "npm test" }, status, text: "" },
      },
    }) as WsEvent

  it("a pending row from PreToolUse appears immediately, with no result", () => {
    // The whole point: a long tool call should read as RUNNING, not as nothing
    // happening. Before PreToolUse was forwarded, the row only appeared once the
    // call had already finished.
    const next = sessionReducer(makeState(), mk("pending"))
    expect(next.messages).toHaveLength(1)
    expect(next.messages[0].role).toBe("tool_use")
    expect(next.messages[0].content.status).toBe("pending")
    // No tool_result row — that's what makes ToolCallPair render "running…".
    expect(next.messages.some((m) => m.role === "tool_result")).toBe(false)
  })

  it("the completed row REPLACES the pending one rather than doubling it", () => {
    const pending = sessionReducer(makeState(), mk("pending"))
    const done = sessionReducer(pending, mk("complete", "seq:-1"))
    expect(done.messages).toHaveLength(1)
    expect(done.messages[0].content.status).toBe("complete")
  })

  it("the durable transcript row then supersedes both", () => {
    // Three deliveries of one call — pending hook, completed hook, transcript —
    // must converge on a single row carrying the durable identity.
    const durable = {
      event: "chat.tool_use",
      data: {
        parent_message_id: null,
        tool_message_id: "991",
        turn_index: 4096,
        block: { id: "toolu_LIVE", name: "Bash", input: { command: "npm test" }, text: "" },
      },
    } as WsEvent
    const settled = sessionReducer(
      sessionReducer(sessionReducer(makeState(), mk("pending")), mk("complete")),
      durable,
    )
    expect(settled.messages).toHaveLength(1)
    expect(settled.messages[0].id).toBe("991")
    expect(settled.messages[0].turn_index).toBe(4096)
  })
})

describe("sessionReducer — a web send arriving twice", () => {
  it("does not render the same message twice at two different ordinals", () => {
    // THE live bug (2026-07-27): a web send writes its row at a DENSE index
    // (_next_index), then the agent reads it and the transcript re-ships the
    // same text at a COMPOSITE ordinal. Different index, different id, so
    // matching on turn_index alone missed and the message appeared twice — while
    // a reload showed it once, because get_or_create dedupes server-side.
    const seeded = makeState({
      messages: [
        makeMessage({ id: "501", turn_index: 37, role: "user", plaintext: "try one more time" }),
      ],
    })
    const fromTranscript = {
      event: "chat.user_message",
      data: { message_id: "902", turn_index: 144448, plaintext: "try one more time" },
    } as WsEvent
    const next = sessionReducer(seeded, fromTranscript)
    expect(next.messages).toHaveLength(1)
    // And it adopts the durable ordinal, so it sorts where a reload puts it.
    expect(next.messages[0].turn_index).toBe(144448)
    expect(next.messages[0].id).toBe("902")
  })

  it("a genuine repeat sent much later is still its own row", () => {
    // The dedupe must not swallow real repetition — only the tail is compared.
    const many = Array.from({ length: 9 }, (_, i) =>
      makeMessage({ id: `u${i}`, turn_index: i, role: "user", plaintext: i === 0 ? "yes" : `m${i}` }),
    )
    const next = sessionReducer(makeState({ messages: many }), {
      event: "chat.user_message",
      data: { message_id: "new", turn_index: 500, plaintext: "yes" },
    } as WsEvent)
    expect(next.messages).toHaveLength(10)
  })

  it("an empty-text frame never collapses onto another empty row", () => {
    const seeded = makeState({
      messages: [makeMessage({ id: "1", turn_index: 1, role: "user", plaintext: "" })],
    })
    const next = sessionReducer(seeded, {
      event: "chat.user_message",
      data: { message_id: "2", turn_index: 2, plaintext: "" },
    } as WsEvent)
    expect(next.messages).toHaveLength(2)
  })
})

describe("blocked — the agent is waiting on YOU", () => {
  it("renders as its own state, not as working", () => {
    const state = sessionReducer(makeState(), {
      event: "session.activity",
      data: { state: "blocked" },
    });
    expect(state.activity).toBe("blocked");
  });

  it("clears once the agent produces a row again", () => {
    // Notification fires on the way INTO a wait and nothing fires on the way
    // out — approving a permission prompt emits no hook at all. A row is the
    // only proof the wait ended, so without this the chip says "needs you" for
    // the rest of the turn, long after you answered.
    const blocked = sessionReducer(makeState(), {
      event: "session.activity",
      data: { state: "blocked" },
    });
    const after = sessionReducer(blocked, {
      event: "chat.tool_use",
      data: {
        parent_message_id: null,
        tool_message_id: "m1",
        turn_index: 3,
        block: { id: "toolu_1", name: "Bash" },
      },
    });
    expect(after.activity).toBe("working");
  });

  it("is not cleared by traffic that proves nothing about the agent", () => {
    // A teammate typing, or presence churn, says nothing about whether the
    // agent is still waiting for an answer.
    const blocked = sessionReducer(makeState(), {
      event: "session.activity",
      data: { state: "blocked" },
    });
    const after = sessionReducer(blocked, {
      event: "presence.joined",
      data: { user_id: 2, display_name: "Someone" },
    });
    expect(after.activity).toBe("blocked");
  });
});
