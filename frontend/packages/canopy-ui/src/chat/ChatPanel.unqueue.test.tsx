// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import type { Message, QueuedMessage, SessionState } from "./protocol";

afterEach(cleanup);

const me = { name: "Alice A", user_id: 1 };
const bo = { name: "Bo B", user_id: 2 };

function mine(clientId: string, text: string): Message {
  return {
    id: `local:${clientId}`, turn_index: 1, role: "user", content: { text, client_id: clientId },
    plaintext: text, status: "pending", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-18T00:00:00Z", author: me,
  } as Message;
}

function queued(turnId: string, clientId: string, text: string, over: Partial<QueuedMessage> = {}): QueuedMessage {
  return { turn_id: turnId, client_id: clientId, author: me, text, sent_at: "", state: "queued", ...over } as QueuedMessage;
}

function state(over: Partial<SessionState>): SessionState {
  return { messages: [], active_draft: null, participants: [], presence_user_ids: [1], current_user_id: 1, ...over } as SessionState;
}

const props = {
  connected: true, currentUserId: 1, onSend: () => undefined, onStop: () => undefined,
  onUpdateDraft: () => undefined, onDiscard: () => undefined,
} as const;

describe("cancelling a queued send", () => {
  it("offers Cancel on my own queued line and withdraws that turn", () => {
    const onUnqueue = vi.fn();
    render(<ChatPanel {...props} onUnqueue={onUnqueue}
      state={state({ messages: [mine("c1", "first")], queued: [queued("t1", "c1", "first")] })} />);
    fireEvent.click(screen.getByTestId("queued-cancel"));
    expect(onUnqueue).toHaveBeenCalledWith("t1");
  });

  it("offers nothing once a runner has it, or for a teammate's line", () => {
    render(<ChatPanel {...props} onUnqueue={vi.fn()} state={state({
      messages: [mine("c1", "first")],
      queued: [queued("t1", "c1", "first", { state: "delivering" }), queued("t2", "c9", "theirs", { author: bo })],
    })} />);
    expect(screen.queryByTestId("queued-cancel")).toBeNull();
  });
});
