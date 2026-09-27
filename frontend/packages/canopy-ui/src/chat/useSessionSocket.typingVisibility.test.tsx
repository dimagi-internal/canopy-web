// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionSocket } from "./useSessionSocket";

/** Per-person typing visibility: a person chooses how their own in-progress
 *  message appears to others (live / typing / hidden). The hook holds the
 *  choice, persists it per browser, and stamps every `draft.update` with it. */

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

function snapshot(presenceIds: number[]) {
  return {
    event: "session.state",
    data: { messages: [], active_draft: DRAFT, participants: [], presence_user_ids: presenceIds, current_user_id: 1 },
  };
}

// A minimal, real Storage-shaped fake — the hook reads/writes it through
// `defaultDraftStorage()` (which returns `window.localStorage`), so replacing
// the global is how a remount is proven to persist across.
function fakeLocalStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
}

let storage: ReturnType<typeof fakeLocalStorage>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
  storage = fakeLocalStorage();
  vi.stubGlobal("localStorage", storage);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const wsUrl = () => "wss://h/x";

function connectedWith(presenceIds: number[]) {
  const hook = renderHook(() => useSessionSocket({ sessionId: "s1", wsUrl }));
  act(() => {
    FakeSocket.last!.onopen?.();
    FakeSocket.last!.receive(snapshot(presenceIds));
  });
  return hook;
}

function draftUpdateFrames() {
  return FakeSocket.last!.sent
    .map((f) => JSON.parse(f))
    .filter((f) => f.action === "draft.update");
}

describe("typingVisibility", () => {
  it("defaults to live", () => {
    const hook = connectedWith([1, 2]);
    expect(hook.result.current.typingVisibility).toBe("live");
  });

  it("every draft.update carries the current visibility", () => {
    const hook = connectedWith([1, 2]);
    act(() => hook.result.current.setTypingVisibility("typing"));
    act(() => hook.result.current.updateDraft("hello"));
    act(() => vi.advanceTimersByTime(200));
    const last = draftUpdateFrames().at(-1)!;
    expect(last.data.visibility).toBe("typing");
  });

  it("persists the mode across a remount", () => {
    const first = connectedWith([1, 2]);
    act(() => first.result.current.setTypingVisibility("hidden"));
    first.unmount();

    const second = connectedWith([1, 2]);
    expect(second.result.current.typingVisibility).toBe("hidden");
  });

  it("re-sends immediately on a mode change when live sync is on and there's a body", () => {
    const hook = connectedWith([1, 2]); // presence > 1 => live sync on
    act(() => hook.result.current.updateDraft("mid thought"));
    act(() => vi.advanceTimersByTime(200)); // flush the debounce
    const before = draftUpdateFrames().length;
    act(() => hook.result.current.setTypingVisibility("hidden"));
    const frames = draftUpdateFrames();
    expect(frames.length).toBeGreaterThan(before);
    expect(frames.at(-1)!.data.visibility).toBe("hidden");
    expect(frames.at(-1)!.data.body).toBe("mid thought");
  });

  it("does not send on a mode change when the draft is empty", () => {
    const hook = connectedWith([1, 2]);
    const before = draftUpdateFrames().length;
    act(() => hook.result.current.setTypingVisibility("typing"));
    expect(draftUpdateFrames().length).toBe(before);
  });

  it("reaches the server on a mode change even while ALONE (privacy fix)", () => {
    // Gating the immediate send on presence left a hole: switch to Hidden
    // while alone, and the server's row stays `live` with the real body —
    // so a peer who joins later reads the words straight off the snapshot.
    const hook = connectedWith([1]); // alone: presence set is just me
    act(() => hook.result.current.updateDraft("mid thought"));
    act(() => vi.advanceTimersByTime(200));
    // Alone, so the keystroke debounce above never actually sent anything.
    expect(draftUpdateFrames().length).toBe(0);
    act(() => hook.result.current.setTypingVisibility("hidden"));
    const frames = draftUpdateFrames();
    expect(frames.length).toBe(1);
    expect(frames[0].data.visibility).toBe("hidden");
    expect(frames[0].data.body).toBe("mid thought");
  });

  it("adopts a mode carried on my own draft.updated frame (other-tab sync)", () => {
    const hook = connectedWith([1, 2]);
    expect(hook.result.current.typingVisibility).toBe("live");
    act(() =>
      FakeSocket.last!.receive({
        event: "draft.updated",
        data: { ...DRAFT, visibility: "hidden" },
      }),
    );
    expect(hook.result.current.typingVisibility).toBe("hidden");
  });
});
