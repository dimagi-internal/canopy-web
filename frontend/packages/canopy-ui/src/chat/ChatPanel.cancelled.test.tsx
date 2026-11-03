// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChatPanel } from "./ChatPanel";
import { withCancelledSends } from "./cancelledSends";
import type { CancelledSend, Message, SessionState } from "./protocol";

afterEach(cleanup);

const me = { name: "Jon", user_id: 1 };

function row(id: string, idx: number, role: "user" | "assistant", text: string, at: string, over: Partial<Message> = {}): Message {
  return {
    id, turn_index: idx, role, content: { text }, plaintext: text, status: "complete",
    error_detail: null, started_at: null, completed_at: null, created_at: at, author: role === "user" ? me : null, ...over,
  } as Message;
}

// canopy-web#2092: A is sent and cancelled; B is sent and answered. A must read
// between the earlier reply and B — not under B's reply — and say it was cancelled.
const cancelledA: CancelledSend = {
  turn_id: "t-a", client_id: "ca", author: me, text: "message A", sent_at: "2026-10-25T10:01:00Z", cancelled_by: "Jon",
};
const transcript = [
  row("m0", 1, "user", "earlier", "2026-10-25T10:00:00Z"),
  row("m1", 2, "assistant", "earlier reply", "2026-10-25T10:00:30Z"),
  row("m2", 3, "user", "message B", "2026-10-25T10:02:00Z"),
  row("m3", 4, "assistant", "reply to B", "2026-10-25T10:02:30Z"),
];

describe("a cancelled send", () => {
  it("is ordered by when it was sent, not pinned to the tail", () => {
    const out = withCancelledSends(transcript, [cancelledA]);
    expect(out.map((m) => m.plaintext)).toEqual(["earlier", "earlier reply", "message A", "message B", "reply to B"]);
  });

  it("replaces the optimistic row of the same send", () => {
    const optimistic = row("local:ca", 5, "user", "message A", "2026-10-25T10:03:00Z", { status: "pending", content: { text: "message A", client_id: "ca" } });
    const out = withCancelledSends([...transcript, optimistic], [cancelledA]);
    expect(out.filter((m) => m.plaintext === "message A")).toHaveLength(1);
    expect(out.map((m) => m.plaintext)).toEqual(["earlier", "earlier reply", "message A", "message B", "reply to B"]);
  });

  it("is left out when it predates the loaded tail", () => {
    const old = { ...cancelledA, sent_at: "2026-10-24T00:00:00Z" };
    expect(withCancelledSends(transcript, [old]).map((m) => m.id)).toEqual(["m0", "m1", "m2", "m3"]);
  });

  it("renders muted with a Cancelled label naming who cancelled it", () => {
    const state = { messages: transcript, cancelled: [cancelledA], active_draft: null, participants: [],
      presence_user_ids: [1], current_user_id: 1 } as unknown as SessionState;
    render(<ChatPanel connected currentUserId={1} onSend={() => undefined} onStop={() => undefined}
      onUpdateDraft={() => undefined} onDiscard={() => undefined} state={state} />);
    const label = screen.getByTestId("message-cancelled");
    expect(label.textContent).toContain("Cancelled by Jon");
    const texts = screen.getAllByText(/message A|message B|reply to B/).map((e) => e.textContent);
    expect(texts).toEqual(["message A", "message B", "reply to B"]);
  });

  it("says a send that failed was not delivered, with the runner's reason", () => {
    const failed: CancelledSend = { ...cancelledA, cancelled_by: "", state: "failed", detail: "runner went away" };
    const state = { messages: transcript, cancelled: [failed], active_draft: null, participants: [],
      presence_user_ids: [1], current_user_id: 1 } as unknown as SessionState;
    render(<ChatPanel connected currentUserId={1} onSend={() => undefined} onStop={() => undefined}
      onUpdateDraft={() => undefined} onDiscard={() => undefined} state={state} />);
    const label = screen.getByTestId("message-cancelled");
    expect(label.textContent).toContain("Not delivered");
    expect(label.textContent).toContain("runner went away");
    expect(label.textContent).not.toContain("Cancelled");
  });
});
