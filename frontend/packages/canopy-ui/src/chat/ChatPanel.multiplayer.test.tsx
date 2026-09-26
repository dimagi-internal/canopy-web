// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChatPanel } from "./ChatPanel";
import type { SessionState } from "./protocol";

afterEach(cleanup);

function state(over: Partial<SessionState> = {}): SessionState {
  return {
    messages: [], active_draft: null, participants: [
      { user_id: 1, email: "a@x", display_name: "Alice A", role: "owner", joined_at: null, last_seen_at: null },
      { user_id: 2, email: "b@x", display_name: "Bo B", role: "editor", joined_at: null, last_seen_at: null },
    ], presence_user_ids: [1, 2], current_user_id: 1, ...over,
  } as SessionState;
}

const noop = () => undefined;
const props = {
  connected: true,
  currentUserId: 1,
  onSend: noop,
  onStop: noop,
  onUpdateDraft: noop,
  onDiscard: noop,
} as const;

// Plain DOM assertions on purpose — this repo does not register
// @testing-library/jest-dom, so toHaveTextContent and friends are not
// available here.
describe("multiplayer composer", () => {
  it("shows a teammate's live text and never locks my box", () => {
    render(<ChatPanel {...props} state={state({ peer_drafts: [{ author: { id: 2, name: "Bo B" }, body: "thinking out loud", at: null }] })} />);
    const row = screen.getByTestId("typing-row");
    expect(row.textContent).toContain("Bo B");
    expect(row.textContent).toContain("thinking out loud");
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).disabled).toBe(false);
    expect(screen.queryByTestId("coedit-banner")).toBeNull();
  });

  it("send stays available while the agent is replying", () => {
    const streaming = { id: "a1", turn_index: 2, role: "assistant" as const, content: {}, plaintext: "working", status: "streaming" as const, error_detail: null, started_at: null, completed_at: null, created_at: "" };
    render(<ChatPanel {...props} state={state({ messages: [streaming] })} />);
    expect(screen.getByRole("button", { name: /stop/i })).not.toBeNull();
    expect(screen.getByTestId("send")).not.toBeNull();
  });

  it("renders a teammate's queued send with their name", () => {
    render(<ChatPanel {...props} state={state({ queued: [{ turn_id: "t", client_id: "c", author: { name: "Bo B", user_id: 2 }, text: "next up", sent_at: "", state: "queued" }] })} />);
    const row = screen.getByTestId("queued-row");
    expect(row.textContent).toContain("Bo B");
    expect(row.textContent).toContain("queued");
  });

  it("labels someone else's message with their name, not mine", () => {
    const mine = { id: "m1", turn_index: 1, role: "user" as const, content: {}, plaintext: "me", status: "complete" as const, error_detail: null, started_at: null, completed_at: null, created_at: "", author: { name: "Alice A", user_id: 1 } };
    const theirs = { ...mine, id: "m2", turn_index: 2, plaintext: "them", author: { name: "Bo B", user_id: 2 } };
    render(<ChatPanel {...props} state={state({ messages: [mine, theirs] })} />);
    expect(screen.getAllByTestId("message-author").map((n) => n.textContent)).toEqual(["Bo B"]);
  });
});
