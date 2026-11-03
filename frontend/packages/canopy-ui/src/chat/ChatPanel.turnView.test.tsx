/**
 * One answer to "is the last turn live" across the panel, and rows that keep
 * their identity while the transcript changes around them.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ChatPanel } from "./ChatPanel";
import type { Message, SessionState, TurnStatus } from "./protocol";

afterEach(cleanup);

const ME = 1;
let n = 0;

function msg(over: Partial<Message> = {}): Message {
  n += 1;
  return {
    id: `m${n}`, turn_index: n, role: "assistant", content: {}, plaintext: "",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-31T10:00:00Z", ...over,
  };
}
function call(name: string, at = "2026-10-31T10:00:00Z", done = true): Message[] {
  const id = `t${(n += 1)}`;
  const use = msg({ role: "tool_use", content: { id, name, input: { command: name } }, created_at: at });
  if (!done) return [use];
  return [use, msg({ role: "tool_result", content: { tool_use_id: id }, plaintext: "out", created_at: at })];
}
function status(over: Partial<TurnStatus> = {}): TurnStatus {
  return {
    state: "done", agent_slug: "hal", runners: ["box"], claimed_by: null, pinned: false,
    cloud_runner: null, cloud_runner_id: null, last_seen_at: null, menu_pending: false,
    settled: true, stuck: false, ...over,
  };
}
function state(over: Partial<SessionState> = {}): SessionState {
  return {
    messages: [],
    active_draft: { id: "d1", body: "", version: 1, last_editor: ME, last_edit_at: "", slot: "next", status: "open" },
    participants: [], presence_user_ids: [ME], current_user_id: ME,
    ...over,
  };
}
function Panel({ s }: { s: SessionState }) {
  return <ChatPanel state={s} connected currentUserId={ME} onSend={vi.fn()} onStop={vi.fn()} onUpdateDraft={vi.fn()} />;
}

describe("a reply whose stream_complete never arrived", () => {
  it("does not hold up 'working' and Stop, and the turn folds, once the server settled it", () => {
    const stuck = msg({ plaintext: "partial reply", status: "streaming", created_at: "2026-10-31T10:00:30Z" });
    render(<Panel s={state({
      messages: [msg({ role: "user", plaintext: "go" }), ...call("Bash"), ...call("Read"), stuck],
      turn_status: status({ finished_at: "2026-10-31T10:02:00Z" }),
      activity: "idle",
    })} />);
    expect(screen.queryByTestId("pending-reply")).toBeNull();
    expect(screen.getByTestId("turn").getAttribute("data-live")).toBe("false");
    expect(screen.getByTestId("turn-fold")).toBeTruthy();
  });

  it("an older streaming row never counts — only the newest assistant row can be in flight", () => {
    render(<Panel s={state({
      messages: [
        msg({ role: "user", plaintext: "one" }),
        msg({ plaintext: "lost", status: "streaming" }),
        msg({ role: "user", plaintext: "two" }),
        msg({ plaintext: "done" }),
      ],
    })} />);
    expect(screen.queryByTestId("pending-reply")).toBeNull();
  });
});

describe("rows keep their identity", () => {
  it("a send's receipt (local id → server id) does not remount its turn", () => {
    const pending = msg({ id: "local:c1", role: "user", plaintext: "hello", status: "pending", content: { client_id: "c1" } });
    const tool = call("Bash");
    const { rerender } = render(<Panel s={state({ messages: [pending, ...tool] })} />);
    const before = screen.getByTestId("turn");
    const confirmed = { ...pending, id: "981", status: "complete" as const };
    rerender(<Panel s={state({ messages: [confirmed, ...tool] })} />);
    expect(screen.getByTestId("turn")).toBe(before);
  });

  it("a live tool row adopting its durable id stays the same row", () => {
    const user = msg({ role: "user", plaintext: "go" });
    const live = msg({ id: "seq:-1", role: "tool_use", content: { id: "tu1", name: "Bash", input: {} } });
    const working = status({ state: "working", settled: false });
    const { rerender } = render(<Panel s={state({ messages: [user, live], turn_status: working })} />);
    const row = screen.getByTestId("tool-row");
    rerender(<Panel s={state({ messages: [user, { ...live, id: "4417", turn_index: 99 }], turn_status: working })} />);
    expect(screen.getByTestId("tool-row")).toBe(row);
  });

  it("a run growing from two calls to three keeps its rows mounted (an opened row stays open)", () => {
    const user = msg({ role: "user", plaintext: "go" });
    const a = call("Bash");
    const b = call("Read");
    const working = status({ state: "working", settled: false });
    const { rerender } = render(<Panel s={state({ messages: [user, ...a, ...b], turn_status: working })} />);
    const first = screen.getAllByTestId("tool-row")[0] as HTMLDetailsElement;
    first.open = true;
    expect(screen.getByTestId("tool-run").getAttribute("data-grouped")).toBe("false");
    rerender(<Panel s={state({ messages: [user, ...a, ...b, ...call("Grep")], turn_status: working })} />);
    expect(screen.getByTestId("tool-run").getAttribute("data-grouped")).toBe("true");
    expect(screen.getAllByTestId("tool-row")[0]).toBe(first);
    expect(first.open).toBe(true);
  });
});

describe("tool rows say their status", () => {
  it("data-tool-status follows the one failure rule", () => {
    const id = "tx";
    render(<Panel s={state({
      messages: [
        msg({ role: "user", plaintext: "go" }),
        msg({ role: "tool_use", content: { id, name: "Bash", input: {} } }),
        msg({ role: "tool_result", content: { tool_use_id: id, is_error: true }, plaintext: "boom" }),
      ],
    })} />);
    expect(screen.getByTestId("tool-row").getAttribute("data-tool-status")).toBe("error");
  });
});

describe("message rows carry their place", () => {
  it("each row says its role and ordinal", () => {
    render(<Panel s={state({ messages: [msg({ role: "user", plaintext: "hi", turn_index: 4 }), msg({ plaintext: "hello", turn_index: 5 })] })} />);
    const rows = screen.getAllByTestId("message-row");
    expect(rows.map((r) => [r.getAttribute("data-role"), r.getAttribute("data-turn-index")])).toEqual([
      ["user", "4"], ["assistant", "5"],
    ]);
  });
});
