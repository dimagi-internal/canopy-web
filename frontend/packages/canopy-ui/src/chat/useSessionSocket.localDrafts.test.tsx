// @vitest-environment jsdom
/**
 * `localDrafts`: a conversation whose words the server must not hold (canopy's private
 * conversations) keeps its draft in the browser — no `draft.update`, even with someone
 * else present — and the text still reaches the server inside the send.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionSocket } from "./useSessionSocket";

class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  constructor(public url: string) {
    FakeSocket.last = this;
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {}
  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

const DRAFT = { id: "d1", slot: "next", status: "open", body: "", version: 1, last_editor: 0, last_edit_at: "" };

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const wsUrl = () => "wss://h/x";

function connected(localDrafts: boolean) {
  const hook = renderHook(() => useSessionSocket({ sessionId: "s1", wsUrl, localDrafts }));
  act(() => {
    FakeSocket.last!.onopen?.();
    FakeSocket.last!.receive({ event: "session.state", data: { messages: [], active_draft: DRAFT, participants: [],
                                                                presence_user_ids: [1, 2], current_user_id: 1 } });
  });
  return hook;
}
const frames = (action: string) =>
  FakeSocket.last!.sent.map((f) => JSON.parse(f)).filter((f) => f.action === action);

describe("localDrafts", () => {
  it("never mirrors keystrokes, with someone else present; the send carries the text", () => {
    const hook = connected(true);
    act(() => hook.result.current.updateDraft("patient Amina"));
    act(() => vi.advanceTimersByTime(2000));
    expect(frames("draft.update")).toEqual([]);
    act(() => hook.result.current.sendChat("patient Amina"));
    expect(frames("chat.send")[0].data.text).toBe("patient Amina");
  });

  it("an ordinary conversation still co-edits with someone present", () => {
    const hook = connected(false);
    act(() => hook.result.current.updateDraft("hello"));
    act(() => vi.advanceTimersByTime(2000));
    expect(frames("draft.update").length).toBeGreaterThan(0);
  });
});
