import { describe, expect, it } from "vitest";
import {
  isBoundRunnerOffline,
  isSessionCapable,
  onlineSessionCapableRunners,
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
