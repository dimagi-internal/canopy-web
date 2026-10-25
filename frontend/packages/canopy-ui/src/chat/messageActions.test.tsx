// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel, editTarget } from "./ChatPanel";
import { MessageItem } from "./MessageItem";
import { ToolCallPair } from "./ToolCallPair";
import { MessageActionsContext, toolCopyText } from "./messageActions";
import type { Message, QueuedMessage, SessionState } from "./protocol";

const me = { name: "Alice A", user_id: 1 };
const bo = { name: "Bo B", user_id: 2 };

function msg(over: Partial<Message>): Message {
  return {
    id: "m1", turn_index: 1, role: "assistant", content: {}, plaintext: "**bold** reply",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-24T00:00:00Z", ...over,
  } as Message;
}

const writeText = vi.fn(() => Promise.resolve());
beforeEach(() => {
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});
afterEach(cleanup);

describe("Copy", () => {
  it("copies an assistant reply's markdown source", async () => {
    render(<MessageItem message={msg({})} />);
    await act(async () => { fireEvent.click(screen.getByTestId("message-action-copy")); });
    expect(writeText).toHaveBeenCalledWith("**bold** reply");
    expect(screen.getByTestId("message-action-copy").getAttribute("aria-label")).toBe("Copied");
  });

  it("copies a tool call as its command and output, without toggling the row", async () => {
    const use = msg({ id: "u", role: "tool_use", content: { id: "t1", name: "Bash", input: { command: "ls" } } });
    const result = msg({ id: "r", role: "tool_result", plaintext: "a.txt" });
    const { container } = render(<ToolCallPair use={use} result={result} />);
    await act(async () => { fireEvent.click(screen.getByTestId("message-action-copy")); });
    expect(writeText).toHaveBeenCalledWith("$ ls\n\na.txt");
    expect(container.querySelector("details")?.hasAttribute("open")).toBe(false);
  });

  it("falls back to the input as JSON for a tool with no command", () => {
    const use = msg({ role: "tool_use", content: { name: "Read", input: { file_path: "/x" } } });
    expect(toolCopyText(use, null)).toBe('{\n  "file_path": "/x"\n}');
  });

  it("is a labelled button in a toolbar, reachable from the keyboard", () => {
    render(<MessageItem message={msg({})} />);
    expect(screen.getByRole("toolbar", { name: "Message actions" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy" }).tabIndex).toBe(0);
  });
});

describe("Fork from here", () => {
  it("is offered only where the host says, and hands it the row", () => {
    const onFork = vi.fn();
    const host = { canFork: (m: Message) => m.id === "m1", onFork };
    render(
      <MessageActionsContext.Provider value={{ host, editableId: null, onEdit: null }}>
        <MessageItem message={msg({})} />
        <MessageItem message={msg({ id: "m2", turn_index: 2 })} />
      </MessageActionsContext.Provider>,
    );
    const forks = screen.getAllByTestId("message-action-fork");
    expect(forks).toHaveLength(1);
    fireEvent.click(forks[0]);
    expect(onFork).toHaveBeenCalledWith(expect.objectContaining({ id: "m1" }), expect.any(Object));
  });

  it("is not offered when the host gives none", () => {
    render(<MessageItem message={msg({})} />);
    expect(screen.queryByTestId("message-action-fork")).toBeNull();
  });
});

function mine(id: string, text: string, over: Partial<Message> = {}): Message {
  return msg({ id, role: "user", plaintext: text, content: { text }, author: me, ...over });
}
function queued(turnId: string, clientId: string, text: string, over: Partial<QueuedMessage> = {}): QueuedMessage {
  return { turn_id: turnId, client_id: clientId, author: me, text, sent_at: "", state: "queued", ...over } as QueuedMessage;
}
const base = { currentUserId: 1, currentContactId: null, canUnqueue: true, settled: true, blocked: false };

describe("editTarget", () => {
  it("is your last line once nothing is running", () => {
    const messages = [mine("a", "first"), msg({ id: "b" }), mine("c", "second"), msg({ id: "d" })];
    expect(editTarget({ ...base, messages, queued: [] })).toEqual({ messageId: "c", queued: null });
  });

  it("skips a teammate's later line, and is nothing while the agent works", () => {
    const messages = [mine("a", "mine"), msg({ id: "b", role: "user", author: bo })];
    expect(editTarget({ ...base, messages, queued: [] })?.messageId).toBe("a");
    expect(editTarget({ ...base, settled: false, messages, queued: [] })).toBeNull();
  });

  it("is your latest send while it is queued and unclaimed, and nothing once a runner has it", () => {
    expect(editTarget({ ...base, settled: false, messages: [], queued: [queued("t1", "c1", "hi")] }))
      .toEqual({ messageId: null, queued: expect.objectContaining({ turn_id: "t1" }) });
    expect(editTarget({ ...base, messages: [], queued: [queued("t1", "c1", "hi", { state: "delivering" })] }))
      .toBeNull();
  });

  it("is nothing when sending is refused", () => {
    expect(editTarget({ ...base, blocked: true, messages: [mine("a", "x")], queued: [] })).toBeNull();
  });
});

function state(over: Partial<SessionState>): SessionState {
  return { messages: [], active_draft: null, participants: [], presence_user_ids: [1], current_user_id: 1, ...over } as SessionState;
}
const props = {
  connected: true, currentUserId: 1, onSend: () => undefined, onStop: () => undefined,
  onUpdateDraft: () => undefined, onDiscard: () => undefined,
} as const;

describe("Edit and resend", () => {
  it("puts your finished last line back in the box; sending is a new turn", () => {
    const onSend = vi.fn();
    render(<ChatPanel {...props} onSend={onSend} draftStorage={null} state={state({
      active_draft: { id: "d1", slot: "next", status: "open", body: "", version: 1, last_editor: 1, last_edit_at: "" } as never,
      messages: [mine("a", "old ask"), msg({ id: "b" }), mine("c", "fix the typo")],
    })} />);
    expect(screen.getAllByTestId("message-action-edit")).toHaveLength(1);
    act(() => { fireEvent.click(screen.getByTestId("message-action-edit")); });
    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(box.value).toBe("fix the typo");
    fireEvent.change(box, { target: { value: "fix the typo, please" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("fix the typo, please");
  });

  it("takes a queued send out of the queue and back into the box", () => {
    const onUnqueue = vi.fn();
    render(<ChatPanel {...props} onUnqueue={onUnqueue} state={state({
      messages: [mine("a", "earlier")], queued: [queued("t1", "c1", "queued words")],
    })} />);
    expect(screen.queryByTestId("message-action-edit")).toBeNull();
    fireEvent.click(screen.getByTestId("queued-edit"));
    expect(onUnqueue).toHaveBeenCalledWith("t1");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("queued words");
  });

  it("is not offered while the agent is replying", () => {
    render(<ChatPanel {...props} state={state({
      messages: [mine("a", "go"), msg({ id: "b", status: "streaming", plaintext: "work" })],
    })} />);
    expect(screen.queryByTestId("message-action-edit")).toBeNull();
  });
});
