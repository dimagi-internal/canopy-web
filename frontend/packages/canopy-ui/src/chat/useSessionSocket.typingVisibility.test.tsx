// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionSocket } from "./useSessionSocket";

/** Per-person typing visibility: a person chooses how their own in-progress
 *  message appears to others (live / typing / hidden). The mode is its OWN
 *  idempotent, unversioned frame (`draft.set_visibility`) — never a field on
 *  the version-guarded `draft.update` keystroke frame, which is exactly what
 *  let a stale keystroke echo race a mode change and downgrade it
 *  server-side (canopy-ui#… "hidden->live->hidden" regression). The hook
 *  holds the choice, persists it per browser, and applies it unconditionally
 *  both ways: every explicit choice reaches the server regardless of
 *  presence or body, and a `draft.updated` echo's visibility is adopted
 *  unconditionally (only an explicit choice — here, another tab, or another
 *  device — ever changes it server-side, so last-choice-wins is simply
 *  correct). */

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

function sentFrames(action: string) {
  return FakeSocket.last!.sent.map((f) => JSON.parse(f)).filter((f) => f.action === action);
}
const draftUpdateFrames = () => sentFrames("draft.update");
const setVisibilityFrames = () => sentFrames("draft.set_visibility");

describe("typingVisibility", () => {
  it("defaults to live", () => {
    const hook = connectedWith([1, 2]);
    expect(hook.result.current.typingVisibility).toBe("live");
  });

  it("persists the mode across a remount", () => {
    const first = connectedWith([1, 2]);
    act(() => first.result.current.setTypingVisibility("hidden"));
    first.unmount();

    const second = connectedWith([1, 2]);
    expect(second.result.current.typingVisibility).toBe("hidden");
  });

  it("choosing a mode sends draft.set_visibility, presence or not, body or not", () => {
    // No gate at all: it's one tiny idempotent frame, unconditional
    // server-side too — there is nothing left to gate on.
    const alone = connectedWith([1]);
    act(() => alone.result.current.setTypingVisibility("hidden"));
    expect(setVisibilityFrames()).toEqual([{ action: "draft.set_visibility", data: { visibility: "hidden" } }]);
  });

  it("a keystroke's draft.update never carries a visibility field", () => {
    const hook = connectedWith([1, 2]);
    act(() => hook.result.current.setTypingVisibility("typing"));
    act(() => hook.result.current.updateDraft("hello"));
    act(() => vi.advanceTimersByTime(200));
    const last = draftUpdateFrames().at(-1)!;
    expect(last.data).toEqual({ version: 1, body: "hello" });
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
    expect(storage.getItem("canopy.chat.typingVisibility")).toBe("hidden");
  });

  it("adopts a LOOSENING carried on my own draft.updated frame too — last choice wins", () => {
    // The mode can only ever change server-side via an explicit choice now
    // (this tab, another tab, or another device), so a looser echo is a real
    // choice made elsewhere, not a stale race — it must be adopted exactly
    // like a tightening one.
    const hook = connectedWith([1, 2]);
    act(() => hook.result.current.setTypingVisibility("hidden"));
    act(() =>
      FakeSocket.last!.receive({
        event: "draft.updated",
        data: { ...DRAFT, visibility: "live" },
      }),
    );
    expect(hook.result.current.typingVisibility).toBe("live");
    expect(storage.getItem("canopy.chat.typingVisibility")).toBe("live");
  });

  it("reconciles a mismatched mode off the very first snapshot, even alone — mode only, no body", () => {
    // Covers a choice made while disconnected, or before the draft existed.
    // Just the mode, on its own idempotent frame — never a draft.update.
    storage.setItem("canopy.chat.typingVisibility", "hidden");
    const hook = renderHook(() => useSessionSocket({ sessionId: "s1", wsUrl }));
    act(() => {
      FakeSocket.last!.onopen?.();
      FakeSocket.last!.receive({
        event: "session.state",
        data: {
          messages: [],
          active_draft: { ...DRAFT, body: "leftover words", version: 5, visibility: "live" },
          participants: [],
          presence_user_ids: [1], // alone
          current_user_id: 1,
        },
      });
    });
    expect(hook.result.current.typingVisibility).toBe("hidden");
    expect(setVisibilityFrames()).toEqual([
      { action: "draft.set_visibility", data: { visibility: "hidden" } },
    ]);
    expect(draftUpdateFrames()).toEqual([]);
  });

  it("does not reconcile when the snapshot already agrees", () => {
    storage.setItem("canopy.chat.typingVisibility", "live");
    const hook = renderHook(() => useSessionSocket({ sessionId: "s1", wsUrl }));
    act(() => {
      FakeSocket.last!.onopen?.();
      FakeSocket.last!.receive({
        event: "session.state",
        data: {
          messages: [],
          active_draft: { ...DRAFT, visibility: "live" },
          participants: [],
          presence_user_ids: [1],
          current_user_id: 1,
        },
      });
    });
    expect(hook.result.current.typingVisibility).toBe("live");
    expect(setVisibilityFrames()).toEqual([]);
  });

  it("a draft_version_mismatch never touches the mode", () => {
    const hook = connectedWith([1, 2]);
    act(() => hook.result.current.setTypingVisibility("hidden"));
    act(() => hook.result.current.updateDraft("secret"));
    act(() => vi.advanceTimersByTime(200));

    act(() =>
      FakeSocket.last!.receive({
        event: "session.error",
        data: {
          code: "draft_version_mismatch",
          message: "Draft changed since your last edit.",
          detail: { current_version: 2, current_body: "secret" },
        },
      }),
    );

    // No automatic resend of anything mode-related — a body conflict has
    // nothing to do with the mode any more.
    expect(hook.result.current.typingVisibility).toBe("hidden");
    expect(setVisibilityFrames()).toHaveLength(1); // only the explicit choice above
  });
});
