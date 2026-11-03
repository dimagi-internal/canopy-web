import { describe, expect, it } from "vitest";

import type { Message, SessionState, TurnStatus } from "./protocol";
import { inFlightRow, turnView } from "./turnView";

let n = 0;
function msg(over: Partial<Message> = {}): Message {
  n += 1;
  return {
    id: `m${n}`, turn_index: n, role: "assistant", content: {}, plaintext: "",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-31T10:00:00Z", ...over,
  };
}

function status(over: Partial<TurnStatus> = {}): TurnStatus {
  return {
    state: "done", agent_slug: null, runners: [], claimed_by: null, pinned: false,
    cloud_runner: null, cloud_runner_id: null, last_seen_at: null, menu_pending: false,
    settled: true, stuck: false, ...over,
  };
}

function state(over: Partial<SessionState> = {}): SessionState {
  return {
    messages: [], active_draft: null, participants: [], presence_user_ids: [], current_user_id: 1,
    ...over,
  };
}

const draft = { id: "d", slot: "next" as const, status: "open" as const, body: "", version: 1, last_editor: 1, last_edit_at: "" };

describe("inFlightRow", () => {
  it("only the newest assistant row can be in flight", () => {
    const stale = msg({ status: "streaming", created_at: "2026-10-31T09:00:00Z" });
    const rows = [msg({ role: "user" }), stale, msg({ role: "user" }), msg({ status: "complete" })];
    expect(inFlightRow(rows, null)).toBeNull();
  });

  it("ignores the newest streaming row once the server settled the turn after it began", () => {
    const row = msg({ status: "streaming", created_at: "2026-10-31T10:00:00Z" });
    expect(inFlightRow([row], status({ finished_at: "2026-10-31T10:05:00Z" }))).toBeNull();
    expect(inFlightRow([row], status({ state: "working", settled: false }))).toBe(row);
  });

  it("keeps a streaming row newer than the settled turn: work typed straight into the session", () => {
    const row = msg({ status: "streaming", created_at: "2026-10-31T10:10:00Z" });
    expect(inFlightRow([row], status({ finished_at: "2026-10-31T10:05:00Z" }))).toBe(row);
  });
});

describe("turnView", () => {
  it("a lost stream_complete on a settled turn shows idle, no Stop, and folds", () => {
    const v = turnView({
      state: state({
        active_draft: draft,
        messages: [msg({ role: "user" }), msg({ status: "streaming", created_at: "2026-10-31T10:00:00Z" })],
        turn_status: status({ finished_at: "2026-10-31T10:01:00Z" }),
        activity: "idle",
      }),
    });
    expect(v.phase).toBe("idle");
    expect(v.inFlight).toBeNull();
    expect(v.canStop).toBe(false);
    expect(v.live).toBe(false);
    expect(v.listLive).toBe(false);
    expect(v.label).toBe("idle");
  });

  it("a working turn is live, stoppable, and names the runner holding it", () => {
    const v = turnView({
      state: state({ active_draft: draft, turn_status: status({ state: "working", settled: false, claimed_by: "box-1" }) }),
      runnerFallback: "bound-box",
    });
    expect(v).toMatchObject({ phase: "working", working: true, live: true, canStop: true, label: "running", runner: "box-1" });
  });

  it("falls back to the host's runner when the server names none", () => {
    expect(turnView({ state: state(), runnerFallback: "bound-box" }).runner).toBe("bound-box");
  });

  it("a viewer with no draft cannot stop", () => {
    const v = turnView({ state: state({ turn_status: status({ state: "working", settled: false }) }) });
    expect(v.working).toBe(true);
    expect(v.canStop).toBe(false);
  });

  it("an answered dialog reads as working, not 'needs you'", () => {
    const blocked = state({ activity: "blocked", turn_status: status({ state: "working", settled: false, menu_pending: true }) });
    expect(turnView({ state: blocked }).label).toBe("needs you");
    expect(turnView({ state: blocked, pendingAnswer: true }).phase).toBe("working");
  });

  it("awaitingReply alone is queued before the server speaks", () => {
    expect(turnView({ state: state(), awaitingReply: true }).phase).toBe("queued");
  });

  it("listLive: with your send drawn in the queue, the previous finished turn is not live", () => {
    const mine = msg({ id: "local:c1", role: "user", status: "pending", content: { client_id: "c1" } });
    const v = turnView({
      state: state({
        messages: [msg({ role: "user" }), msg({ plaintext: "done" }), mine],
        queued: [{ turn_id: "t", client_id: "c1", author: null, text: "x", sent_at: "", state: "queued" } as never],
        turn_status: status({ state: "waiting_runner", settled: false }),
      }),
    });
    expect(v.live).toBe(true);
    expect(v.listLive).toBe(false);
  });

  it("a background agent started by the last turn keeps it live", () => {
    const use = msg({ role: "tool_use", content: { id: "tu1", name: "Agent", input: {} } });
    const v = turnView({
      state: state({
        messages: [msg({ role: "user" }), use, msg({ plaintext: "launched" })],
        turn_status: status(),
        subagents: [{ agent_id: "a", tool_use_id: "tu1", state: "running" } as never],
      }),
    });
    expect(v.live).toBe(true);
  });
});
