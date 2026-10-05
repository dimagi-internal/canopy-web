import { describe, expect, it } from "vitest";

import type { TurnStatus } from "./protocol";
import { agentHasFloor, pendingLabel, turnNotice } from "./turnStatus";

/** A status in the shape the server actually sends (`TurnStatus.as_dict`). */
function status(over: Partial<TurnStatus> = {}): TurnStatus {
  return {
    state: "working",
    agent_slug: "hal",
    runners: ["jj-mbp"],
    claimed_by: "jj-mbp",
    pinned: false,
    cloud_runner: null,
    cloud_runner_id: null,
    last_seen_at: null,
    menu_pending: false,
    settled: false,
    stuck: false,
    ...over,
  };
}

describe("agentHasFloor", () => {
  it("shows the bubble the instant you press send, before any frame comes back", () => {
    // Nothing server-side can answer here yet — the turn is not enqueued.
    expect(agentHasFloor({ awaitingReply: true })).toBe(true);
  });

  it("keeps it up across the gaps between an assistant's text blocks", () => {
    expect(agentHasFloor({ activity: "working", awaitingReply: false })).toBe(true);
  });

  it("never shows it for an agent waiting on YOU", () => {
    expect(agentHasFloor({ activity: "blocked", awaitingReply: true })).toBe(false);
  });

  it("withdraws it once the server says nothing will pick the turn up", () => {
    // The regression this whole projection exists for: a spinner over an ask
    // queued behind a closed laptop, which used to spin until the tab closed.
    const s = status({ state: "waiting_runner", stuck: true, claimed_by: null });
    expect(agentHasFloor({ status: s, awaitingReply: true })).toBe(false);
  });

  it("withdraws it for an unroutable turn even while the client still thinks it is waiting", () => {
    const s = status({ state: "unrouted", stuck: true, runners: [], claimed_by: null });
    expect(agentHasFloor({ status: s, awaitingReply: true })).toBe(false);
  });

  it("withdraws it once the turn is settled, even if a stream frame was missed", () => {
    // `awaitingReply` only clears on a stream frame. A dropped socket used to
    // leave it true forever; the server's settled flag is the backstop.
    const s = status({ state: "done", settled: true });
    expect(agentHasFloor({ status: s, awaitingReply: true })).toBe(false);
  });

  it("shows it for a queued turn a live runner is taking", () => {
    expect(agentHasFloor({ status: status({ state: "picking_up" }) })).toBe(true);
  });

  it("lets the server override a stale client belief that nothing is happening", () => {
    // Someone else sent on this session from another device.
    expect(agentHasFloor({ status: status({ state: "working" }), awaitingReply: false })).toBe(true);
  });
});

describe("pendingLabel", () => {
  it("says where the delay IS, not merely that there is one", () => {
    expect(pendingLabel({ status: status({ state: "picking_up" }) })).toBe(
      "Picking this up on jj-mbp…",
    );
    expect(pendingLabel({ status: status({ state: "working" }) })).toBe("Thinking on jj-mbp…");
  });

  it("says a follow-up is queued behind the turn ahead, not being picked up (#1147)", () => {
    const s = status({ state: "queued_behind", behind: "session", claimed_by: null });
    expect(agentHasFloor({ status: s })).toBe(true);
    expect(pendingLabel({ status: s })).toBe("Queued behind the previous message on jj-mbp…");
    expect(pendingLabel({ status: { ...s, behind: "agent" } })).toBe(
      "Queued behind the agent's current turn on jj-mbp…",
    );
    expect(turnNotice(s)).toBeNull();
  });

  it("names a pinned runner as a decision, not a queue position", () => {
    const s = status({ state: "picking_up", pinned: true, runners: ["cloud-1"] });
    expect(pendingLabel({ status: s })).toBe("Sent to cloud-1…");
  });

  it("falls back to the coarse wording with no status in hand", () => {
    expect(pendingLabel({ activity: "working" })).toBe("Thinking…");
    expect(pendingLabel({})).toBe("Queued…");
  });
});

describe("turnNotice", () => {
  it("says nothing when there is nothing a person needs to do", () => {
    expect(turnNotice(null)).toBeNull();
    expect(turnNotice(status({ state: "working" }))).toBeNull();
    expect(turnNotice(status({ state: "picking_up" }))).toBeNull();
    expect(turnNotice(status({ state: "done", settled: true }))).toBeNull();
  });

  it("leaves a blocked agent to the menu, which has buttons", () => {
    // A sentence beside the dialog would say the same thing twice and compete
    // with the thing you can actually press.
    expect(turnNotice(status({ state: "blocked", stuck: true }))).toBeNull();
  });

  it("names the offline box, because the fix is to go and open it", () => {
    const n = turnNotice(status({ state: "waiting_runner", stuck: true, claimed_by: null }));
    expect(n?.tone).toBe("warn");
    expect(n?.text).toContain("jj-mbp is offline");
  });

  it("reads differently for the state waiting cannot fix", () => {
    const n = turnNotice(status({ state: "unrouted", stuck: true, runners: [], claimed_by: null }));
    // Not a warning you can wait out — an error somebody has to go and fix.
    expect(n?.tone).toBe("error");
    expect(n?.text).toContain("no runner is set up to run hal");
  });

  it("carries the rescue offer when moving it would help", () => {
    const n = turnNotice(
      status({
        state: "waiting_runner",
        stuck: true,
        cloud_runner: "cloud-1",
        cloud_runner_id: "8f2e",
      }),
    );
    expect(n?.offer).toEqual({ name: "cloud-1", id: "8f2e" });
  });

  it("offers nothing when the server named no runner to move it to", () => {
    const n = turnNotice(status({ state: "waiting_runner", stuck: true }));
    expect(n?.offer).toBeNull();
  });

  it("reports a box that died mid-turn, which the turn itself still calls running", () => {
    const n = turnNotice(status({ state: "paused", stuck: true }));
    expect(n?.text).toContain("jj-mbp went offline mid-turn");
  });

  it("degrades to no notice for a state it has never heard of", () => {
    // An older client meeting a newer server must fall silent, not crash.
    expect(turnNotice(status({ state: "some_future_state" }))).toBeNull();
  });

  it("says why a turn failed when the runner gave a reason", () => {
    // 2026-09-27: a bare "Could not finish." hid a note that said exactly
    // what to do.
    const n = turnNotice(status({
      state: "failed", settled: true,
      detail: "Your message was not delivered: the emdash session has unsent text in its prompt.",
    }));
    expect(n?.tone).toBe("error");
    expect(n?.text).toBe(
      "Could not finish — Your message was not delivered: the emdash session has unsent text in its prompt.",
    );
  });

  it("keeps the bare wording for a failure with no reason, or an older server", () => {
    expect(turnNotice(status({ state: "failed", settled: true }))?.text).toBe("Could not finish.");
    expect(turnNotice(status({ state: "failed", settled: true, detail: null }))?.text)
      .toBe("Could not finish.");
  });

  it("drops a settled failure once the conversation has moved past it", () => {
    // Work typed straight into the session makes messages but no Turn, so the
    // latest Turn can be a day-old failure under a thread that is fine.
    const failed = status({
      state: "failed", settled: true, finished_at: "2026-09-26T22:07:07Z",
    });
    expect(turnNotice(failed, { latestMessageAt: "2026-09-27T17:21:38Z" })).toBeNull();
    expect(turnNotice(status({
      state: "lost", settled: true, finished_at: "2026-09-26T22:07:07Z",
    }), { latestMessageAt: "2026-09-27T17:21:38Z" })).toBeNull();
  });

  it("still shows a failure that is the conversation's last word", () => {
    const failed = status({
      state: "failed", settled: true, finished_at: "2026-09-26T22:07:07Z",
    });
    expect(turnNotice(failed, { latestMessageAt: "2026-09-26T22:06:01Z" })?.tone).toBe("error");
    expect(turnNotice(failed, { latestMessageAt: null })?.tone).toBe("error");
    expect(turnNotice(failed)?.tone).toBe("error");
  });

  it("never lets a newer message hide a turn that is not settled", () => {
    // A live or stuck turn is still owed something, whatever else was said.
    const n = turnNotice(
      status({ state: "waiting_runner", stuck: true, finished_at: "2026-09-26T22:07:07Z" }),
      { latestMessageAt: "2026-09-27T17:21:38Z" },
    );
    expect(n?.tone).toBe("warn");
  });
});

describe("turnNotice, when the conversation requires a runner flag", () => {
  it("names the missing requirement when unrouted", () => {
    const n = turnNotice(status({ state: "unrouted", stuck: true, runners: [], claimed_by: null, agent_slug: "ace", requires: ["zdr"] }));
    expect(n?.text).toContain("ZDR runner");
  });

  it("names it when its runners are offline", () => {
    const n = turnNotice(status({ state: "waiting_runner", stuck: true, runners: ["cloud-1"], claimed_by: null, requires: ["zdr"] }));
    expect(n?.text).toContain("ZDR");
  });
});
