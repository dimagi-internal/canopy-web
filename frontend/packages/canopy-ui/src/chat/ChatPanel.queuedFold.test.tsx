// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChatPanel } from "./ChatPanel";
import type { Message, QueuedMessage, SessionState, TurnStatus } from "./protocol";

afterEach(cleanup);

const me = { name: "Alice A", user_id: 1 };
const row = (o: Partial<Message>): Message => ({
  id: "x", turn_index: 1, role: "user", content: {}, plaintext: "", status: "complete",
  error_detail: null, started_at: null, completed_at: null, created_at: "", ...o,
} as Message);

// A finished turn: your line, a tool call, the reply. It folds.
const finished = [
  row({ id: "u1", turn_index: 1, plaintext: "do it", content: { text: "do it" }, author: me }),
  row({ id: "t1", turn_index: 2, role: "tool_use", content: { id: "tu", name: "Bash", input: { command: "ls" } } }),
  row({ id: "r1", turn_index: 3, role: "tool_result", content: { tool_use_id: "tu", content: "ok" } }),
  row({ id: "a1", turn_index: 4, role: "assistant", plaintext: "done", content: { text: "done" } }),
];
const send = row({
  id: "local:c1", turn_index: 5, plaintext: "next", content: { text: "next", client_id: "c1" },
  status: "pending", author: me,
});
const inQueue: QueuedMessage = { turn_id: "t", client_id: "c1", author: me, text: "next", sent_at: "", state: "queued" };
const offline = {
  state: "waiting_runner", agent_slug: "hal", runners: ["jj-mbp"], claimed_by: null, pinned: false,
  cloud_runner: null, cloud_runner_id: null, last_seen_at: null, menu_pending: false,
  settled: false, stuck: false,
} as TurnStatus;

const props = {
  connected: true, currentUserId: 1, onSend: () => undefined, onStop: () => undefined,
  onUpdateDraft: () => undefined, onDiscard: () => undefined,
} as const;
const state = (o: Partial<SessionState>) =>
  ({ messages: [], active_draft: null, participants: [], presence_user_ids: [1], current_user_id: 1, ...o }) as SessionState;

describe("a send waiting in the queue", () => {
  it("leaves the finished turn above it folded", () => {
    // It used to unfold the whole previous turn while the send sat in the
    // queue and fold it back when a runner took it: the page shrank by that
    // much at once and could paint blank until scrolled.
    render(<ChatPanel {...props} awaitingReply
      state={state({ messages: [...finished, send], queued: [inQueue], turn_status: offline })} />);
    expect(screen.getByTestId("queued-row").textContent).toContain("next");
    expect(screen.getAllByTestId("turn-fold")).toHaveLength(1);
  });

  it("still shows the turn above it unfolded while that turn is running", () => {
    render(<ChatPanel {...props} awaitingReply
      state={state({ messages: [...finished, send], queued: [inQueue], turn_status: offline, activity: "working" })} />);
    expect(screen.queryByTestId("turn-fold")).toBeNull();
  });
});
