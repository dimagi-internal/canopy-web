// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("shows 'is typing…' with no words when a teammate withholds them", () => {
    render(<ChatPanel {...props} state={state({ peer_drafts: [{ author: { id: 2, name: "Bo B" }, body: "", at: null, typing: true }] })} />);
    const row = screen.getByTestId("typing-row");
    expect(row.textContent).toContain("Bo B");
    expect(row.textContent).toContain("is typing…");
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

  it("shows exactly one row for the sender's own HTTP/contact send", () => {
    // `noteLocalSend`'s optimistic row and the server's queued-turn
    // projection describe the SAME send once both carry the same client_id —
    // an HTTP send (a contact's, or the widget's first message) has no
    // `author.user_id` to compare against `currentUserId`, so client_id
    // matching (`hideClientIds`) is the only thing that can tell them apart
    // from two different people.
    const mine = {
      id: "local:1", turn_index: 1, role: "user" as const,
      content: { text: "please help", client_id: "abc" }, plaintext: "please help",
      status: "complete" as const, error_detail: null, started_at: null,
      completed_at: null, created_at: "",
    };
    render(<ChatPanel {...props} state={state({
      messages: [mine],
      queued: [{ turn_id: "t", client_id: "abc", author: null, text: "please help", sent_at: "", state: "queued" }],
    })} />);
    expect(screen.queryByTestId("queued-row")).toBeNull();
    expect(screen.getAllByText("please help")).toHaveLength(1);
  });

  it("labels someone else's message with their name, not mine", () => {
    const mine = { id: "m1", turn_index: 1, role: "user" as const, content: {}, plaintext: "me", status: "complete" as const, error_detail: null, started_at: null, completed_at: null, created_at: "", author: { name: "Alice A", user_id: 1 } };
    const theirs = { ...mine, id: "m2", turn_index: 2, plaintext: "them", author: { name: "Bo B", user_id: 2 } };
    render(<ChatPanel {...props} state={state({ messages: [mine, theirs] })} />);
    expect(screen.getAllByTestId("message-author").map((n) => n.textContent)).toEqual(["Bo B"]);
  });
});

describe("a contact's failed send restores what they typed", () => {
  // A contact's socket is read-only, so their send goes over HTTP
  // (`useSessionSocket.sendChatOverHttp`) and `current_user_id` is null on
  // their connect snapshot (consumers.py: "None for a contact, who has no
  // user id"). Their composer's `active_draft` is the hook's own LOCAL
  // stand-in (`id: "local"`) rather than a server-assigned draft.
  function localDraft(body: string) {
    return {
      id: "local", slot: "next" as const, status: "open" as const, body,
      version: 0, last_editor: 0, last_edit_at: new Date().toISOString(),
    };
  }

  it("shows the text again once the HTTP send fails", () => {
    const contactProps = { ...props, currentUserId: null };
    const composer = () => screen.getByTestId("composer") as HTMLTextAreaElement;

    const { rerender } = render(
      <ChatPanel {...contactProps} state={state({
        active_draft: localDraft("please help"), current_user_id: null,
      })} />,
    );
    expect(composer().value).toBe("please help");

    fireEvent.click(screen.getByTestId("send"));
    expect(composer().value).toBe("");

    // The send went out — `sendChatOverHttp` clears the hook's local draft to
    // null the instant it fires, well before the HTTP round trip resolves.
    rerender(
      <ChatPanel {...contactProps} state={state({
        active_draft: null, current_user_id: null,
      })} />,
    );
    expect(composer().value).toBe("");

    // The HTTP send failed — the hook's catch handler re-seeds the SAME local
    // draft with the original text ("Put the words back, so a failed send
    // never costs what was typed.").
    rerender(
      <ChatPanel {...contactProps} state={state({
        active_draft: localDraft("please help"), current_user_id: null,
      })} />,
    );
    expect(composer().value).toBe("please help");
  });
});

describe("a contact viewer (final review C2 / I6)", () => {
  // A widget visitor has no user id: `current_user_id` is null and the
  // snapshot names them by `current_contact_id` instead.
  const contactState = (over: Partial<SessionState> = {}) =>
    state({ current_user_id: null, current_contact_id: 7, ...over });
  const contactProps = { ...props, currentUserId: null };
  const authored = (over: Record<string, unknown>) => ({
    id: "m1", turn_index: 1, role: "user" as const, content: {}, plaintext: "hello",
    status: "complete" as const, error_detail: null, started_at: null, completed_at: null,
    created_at: "", ...over,
  });

  it("renders the contact's own authored line as theirs: right-aligned, no label", () => {
    render(<ChatPanel {...contactProps} state={contactState({
      messages: [authored({ author: { name: "Beth", contact_id: 7 } })],
    })} />);
    expect(screen.queryByTestId("message-author")).toBeNull();
    const bubble = screen.getByText("hello").closest("div.rounded-2xl") as HTMLElement;
    expect(bubble.className).toContain("ml-auto");
  });

  it("still labels a member's line for the contact", () => {
    render(<ChatPanel {...contactProps} state={contactState({
      messages: [authored({ author: { name: "Alice A", user_id: 1 } })],
    })} />);
    expect(screen.getAllByTestId("message-author").map((n) => n.textContent)).toEqual(["Alice A"]);
  });

  it("calls the contact's own queued send 'You'", () => {
    render(<ChatPanel {...contactProps} state={contactState({
      queued: [{ turn_id: "t", client_id: "c", author: { name: "Beth", contact_id: 7 }, text: "later", sent_at: "", state: "queued" }],
    })} />);
    const row = screen.getByTestId("queued-row");
    expect(row.textContent).toContain("You");
    expect(row.textContent).not.toContain("Beth");
  });

  it("does not double an own send whose client never sent a client_id", () => {
    // An older host (ace-web's sendOverHttp) sends no client_id, so the queued
    // entry cannot be matched by id. Its optimistic row is still on screen.
    const pending = authored({ id: "local:x", plaintext: "please help", status: "complete",
                               content: { text: "please help", client_id: "x" } });
    render(<ChatPanel {...contactProps} state={contactState({
      messages: [pending],
      queued: [{ turn_id: "t", client_id: "", author: { name: "Beth", contact_id: 7 }, text: "please help ", sent_at: "", state: "queued" }],
    })} />);
    expect(screen.queryByTestId("queued-row")).toBeNull();
  });

  it("a peer's identical text is not hidden by my own optimistic row", () => {
    const pending = authored({ id: "local:x", plaintext: "yes", status: "pending",
                               content: { text: "yes", client_id: "x" } });
    render(<ChatPanel {...contactProps} state={contactState({
      messages: [pending],
      queued: [{ turn_id: "t", client_id: "", author: { name: "Alice A", user_id: 1 }, text: "yes", sent_at: "", state: "queued" }],
    })} />);
    expect(screen.getByTestId("queued-row").textContent).toContain("Alice A");
  });
});

describe("a peer's bubble (final review m4)", () => {
  it("does not wear the agent's treatment", () => {
    const theirs = { id: "m2", turn_index: 2, role: "user" as const, content: {}, plaintext: "them", status: "complete" as const, error_detail: null, started_at: null, completed_at: null, created_at: "", author: { name: "Bo B", user_id: 2 } };
    const agent = { ...theirs, id: "a1", turn_index: 3, role: "assistant" as const, plaintext: "agent", author: null };
    render(<ChatPanel {...props} state={state({ messages: [theirs, agent] })} />);
    const peer = screen.getByText("them").closest("div.rounded-2xl") as HTMLElement;
    const bot = screen.getByText("agent").closest("div.rounded-2xl") as HTMLElement;
    expect(peer.className).not.toBe(bot.className);
    expect(peer.className).toContain("border");
  });
});
