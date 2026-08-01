import { describe, expect, it } from "vitest";
import {
  isBoundRunnerOffline,
  isSessionCapable,
  findBoundRunner,
  onlineSessionCapableRunners,
  parkedReason,
  parkedSummary,
  partitionByRunnerReachability,
} from "./runnerEligibility";
import type { RunnerOut } from "@/api/harness";

function runner(overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id: "r1",
    name: "Runner One",
    kind: "emdash",
    status: "online",
    status_note: "",
    ready: true,
    ready_note: "",
    paused: false,
    paused_note: "",
    paused_at: null,
    last_heartbeat_at: null,
    capabilities: { sessions: true },
    host: "host",
    code_branch: "main",
    code_version: "",
    code_sha: "",
    code_committed_at: 0,
    expected_code_committed_at: 0,
    expected_code_sha: "",
    workspace: null,
    paired_by_email: null,
    can_manage: true,
    ...overrides,
  };
}

describe("isSessionCapable", () => {
  it("is true when capabilities.sessions is exactly true", () => {
    expect(isSessionCapable(runner({ capabilities: { sessions: true } }))).toBe(true);
  });

  it("is false when capabilities.sessions is missing", () => {
    expect(isSessionCapable(runner({ capabilities: {} }))).toBe(false);
  });

  it("is false for a truthy-but-not-boolean-true value", () => {
    expect(isSessionCapable(runner({ capabilities: { sessions: "yes" } }))).toBe(false);
  });
});

describe("onlineSessionCapableRunners", () => {
  it("keeps only online + session-capable runners", () => {
    const fleet = [
      runner({ id: "a", status: "online", capabilities: { sessions: true } }),
      runner({ id: "b", status: "offline", capabilities: { sessions: true } }),
      runner({ id: "c", status: "online", capabilities: {} }),
      runner({ id: "d", status: "online", capabilities: { sessions: true } }),
    ];
    expect(onlineSessionCapableRunners(fleet).map((r) => r.id)).toEqual(["a", "d"]);
  });

  it("returns an empty array for an empty fleet", () => {
    expect(onlineSessionCapableRunners([])).toEqual([]);
  });
});

describe("isBoundRunnerOffline", () => {
  const fleet = [
    runner({ name: "Alpha", status: "online" }),
    runner({ name: "Beta", status: "offline" }),
  ];

  it("is false when there is no bound runner name", () => {
    expect(isBoundRunnerOffline(null, fleet)).toBe(false);
    expect(isBoundRunnerOffline(undefined, fleet)).toBe(false);
    expect(isBoundRunnerOffline("", fleet)).toBe(false);
  });

  it("is false when the bound runner matches an online fleet entry", () => {
    expect(isBoundRunnerOffline("Alpha", fleet)).toBe(false);
  });

  it("is true when the bound runner matches a non-online fleet entry", () => {
    expect(isBoundRunnerOffline("Beta", fleet)).toBe(true);
  });

  it("fails quiet (false) when the fleet list has not loaded", () => {
    expect(isBoundRunnerOffline("Unknown Runner", [])).toBe(false);
  });

  it("is true when a LOADED fleet does not contain the bound runner", () => {
    // GET /runners/ omits retired runners, so absence from a loaded fleet means
    // the runner can never claim again — the banner must offer placement rather
    // than leaving the send queued forever.
    expect(isBoundRunnerOffline("Retired Runner", fleet)).toBe(true);
  });

  it("prefers the server's runner_online over the fleet heuristic", () => {
    // Authoritative: no name matching, no fleet fetch, no staleness.
    expect(isBoundRunnerOffline("Alpha", fleet, false)).toBe(true);
    expect(isBoundRunnerOffline("Retired Runner", fleet, true)).toBe(false);
    expect(isBoundRunnerOffline("Beta", [], true)).toBe(false);
  });
});

describe("parkedReason", () => {
  it("is null for a reachable runner", () => {
    expect(parkedReason({ runner_online: true, runner_status: "online" })).toBeNull();
  });

  it("is null when there is no binding — nothing to be offline", () => {
    // A web chat that has never sent has no runner yet and gets one when it
    // does; treating it as parked would hide a chat the moment you made it.
    expect(parkedReason({})).toBeNull();
    expect(parkedReason({ runner_online: null, runner_status: null })).toBeNull();
  });

  it("names a pause as a pause, not as generic offline", () => {
    expect(parkedReason({ runner_online: false, runner_status: "paused" })).toBe("paused");
  });

  it("calls every other unreachable state offline", () => {
    for (const status of ["stale", "degraded", "disconnected", "retired"]) {
      expect(parkedReason({ runner_online: false, runner_status: status })).toBe("offline");
    }
    // An older payload carries no runner_status at all; the bool still decides.
    expect(parkedReason({ runner_online: false })).toBe("offline");
  });
});

describe("partitionByRunnerReachability", () => {
  it("splits live from parked and preserves order within each side", () => {
    const rows = [
      { id: "a", runner_online: true },
      { id: "b", runner_online: false, runner_status: "paused" },
      { id: "c" },
      { id: "d", runner_online: false, runner_status: "stale" },
    ];
    const { live, parked } = partitionByRunnerReachability(rows);
    expect(live.map((r) => r.id)).toEqual(["a", "c"]);
    expect(parked.map((r) => r.id)).toEqual(["b", "d"]);
  });
});

describe("parkedSummary", () => {
  it("is empty when nothing is held back", () => {
    expect(parkedSummary([])).toBe("");
  });

  it("names the reason when they agree", () => {
    expect(
      parkedSummary([
        { runner_online: false, runner_status: "paused" },
        { runner_online: false, runner_status: "paused" },
      ]),
    ).toBe("2 hidden — runner paused");
  });

  it("stays generic when a pause and a dead box are both in there", () => {
    expect(
      parkedSummary([
        { runner_online: false, runner_status: "paused" },
        { runner_online: false, runner_status: "disconnected" },
      ]),
    ).toBe("2 hidden — runner unavailable");
  });
});

describe("findBoundRunner", () => {
  const fleet = [runner({ id: "r1", name: "Alpha" }), runner({ id: "r2", name: "Beta" })];

  it("returns the fleet row matching the session's runner name", () => {
    expect(findBoundRunner("Beta", fleet)?.id).toBe("r2");
  });

  it("is null when the session names no runner", () => {
    expect(findBoundRunner(null, fleet)).toBeNull();
    expect(findBoundRunner("", fleet)).toBeNull();
  });

  it("is null when the runner is not in the caller's fleet", () => {
    // Retired, or paired by someone else — either way there is no id to act on
    // and no permission to act with, which is what the caller does with null.
    expect(findBoundRunner("Gamma", fleet)).toBeNull();
    expect(findBoundRunner("Alpha", [])).toBeNull();
  });
});

describe("parkedSummary — the hidden ones that are waiting", () => {
  it("names them, so a whole-list waiting count is followable", () => {
    // The heading counts "N waiting on you" over every session while this
    // filter hides part of the list. Reported 2026-08-01: the phone said
    // something was pending and showed nothing pending, with no next move.
    expect(
      parkedSummary([
        { runner_online: false, runner_status: "paused", waiting_on_you: true },
        { runner_online: false, runner_status: "paused" },
      ]),
    ).toBe("2 hidden — runner paused · 1 waiting on you");
  });

  it("stays quiet when nothing hidden is waiting", () => {
    expect(
      parkedSummary([{ runner_online: false, runner_status: "offline" }]),
    ).toBe("1 hidden — runner offline");
  });
});
