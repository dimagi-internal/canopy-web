import { describe, it, expect } from "vitest";
import type { Turn } from "./turnLog";
import { originLabel, agentLabel, matchesTurnFilters, relativeTime } from "./turnLog";

function turn(over: Partial<Turn>): Turn {
  return {
    id: "00000000-0000-0000-0000-000000000000",
    agent_slug: "eva",
    project: "",
    target: "eva",
    workspace_slug: "alpha",
    origin: "api",
    status: "done",
    routing: "prefer_local",
    prompt: "",
    origin_ref: {},
    claimed_by_name: "jj-mbp",
    enqueued_by_email: "jj@dimagi.com",
    session_id: "",
    result_note: "",
    created_at: "2026-07-20T18:00:00Z",
    claimed_at: null,
    started_at: null,
    finished_at: null,
    lease_expires_at: null,
    ...over,
  } as Turn;
}

describe("agentLabel", () => {
  it("uses the agent slug for agent turns", () => {
    expect(agentLabel(turn({ agent_slug: "eva" }))).toBe("eva");
  });
  it("falls back to project:<name> for project turns", () => {
    expect(agentLabel(turn({ agent_slug: null, project: "canopy-web" }))).toBe("project:canopy-web");
  });
});

describe("originLabel", () => {
  it("surfaces the fired slot for a scheduler turn", () => {
    const t = turn({ origin: "canopy_scheduler", origin_ref: { slot: "2026-07-20T13:00:00Z" } });
    expect(originLabel(t)).toContain("canopy_scheduler");
    expect(originLabel(t)).toContain("2026"); // the slot is surfaced
  });
  it("names an off-cycle scheduler run", () => {
    // run_schedule_now is scheduler work too — WHO fired it is not a different
    // source, so the distinction lives in origin_ref, not in origin.
    const t = turn({ origin: "canopy_scheduler", origin_ref: { manual: true } });
    expect(originLabel(t)).toContain("run now");
  });
  it("names the launcher when a human enqueued it", () => {
    expect(originLabel(turn({ origin: "api", enqueued_by_email: "jj@dimagi.com" }))).toContain("jj@dimagi.com");
  });
  it("passes email / api through as the bare origin", () => {
    expect(originLabel(turn({ origin: "email", enqueued_by_email: null }))).toBe("email");
    expect(originLabel(turn({ origin: "api", enqueued_by_email: null }))).toBe("api");
  });
});

describe("matchesTurnFilters", () => {
  const t = turn({ agent_slug: "eva", origin: "canopy_scheduler", status: "done" });
  it("matches when all filters are null (identity)", () => {
    expect(matchesTurnFilters(t, { agent: null, origin: null, status: null })).toBe(true);
  });
  it("matches when every set filter agrees", () => {
    expect(matchesTurnFilters(t, { agent: "eva", origin: "canopy_scheduler", status: "done" })).toBe(true);
  });
  it("rejects when any set filter disagrees (AND)", () => {
    expect(matchesTurnFilters(t, { agent: "eva", origin: "api", status: null })).toBe(false);
  });
  it("filters project turns by their project:<name> agent label", () => {
    const p = turn({ agent_slug: null, project: "canopy-web" });
    expect(matchesTurnFilters(p, { agent: "project:canopy-web", origin: null, status: null })).toBe(true);
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-07-20T18:00:00Z");
  it("reads 'just now' within a minute", () => {
    expect(relativeTime("2026-07-20T17:59:30Z", now)).toBe("just now");
  });
  it("reads minutes", () => {
    expect(relativeTime("2026-07-20T17:45:00Z", now)).toBe("15m ago");
  });
  it("reads hours", () => {
    expect(relativeTime("2026-07-20T16:00:00Z", now)).toBe("2h ago");
  });
  it("reads days", () => {
    expect(relativeTime("2026-07-18T18:00:00Z", now)).toBe("2d ago");
  });
});
