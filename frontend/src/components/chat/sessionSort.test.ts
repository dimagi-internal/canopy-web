import { describe, expect, it } from "vitest";
import { projectHeader, sortSessions } from "./sessionSort";

const row = (
  project: string,
  last_activity_at: string,
  running = false,
  id = `${project}-${last_activity_at}`,
) => ({ id, project, last_activity_at, running });

const T = {
  now: "2026-07-23T22:00:00Z",
  min5: "2026-07-23T21:55:00Z",
  hr4: "2026-07-23T18:00:00Z",
  wk1: "2026-07-16T18:00:00Z",
};

describe("sortSessions — time", () => {
  it("orders by most recent activity, not creation", () => {
    const rows = [row("reef", T.wk1), row("canopy-web", T.now), row("ace", T.hr4)];
    expect(sortSessions(rows, "time").map((r) => r.project)).toEqual([
      "canopy-web",
      "ace",
      "reef",
    ]);
  });

  it("does not mutate the input", () => {
    const rows = [row("b", T.wk1), row("a", T.now)];
    const before = rows.map((r) => r.project);
    sortSessions(rows, "time");
    expect(rows.map((r) => r.project)).toEqual(before);
  });
});

describe("sortSessions — project", () => {
  it("groups by project, then running, then recency within a project", () => {
    const rows = [
      row("reef", T.hr4),
      row("ace", T.wk1),
      row("ace", T.min5, true), // running -> top of its project
      row("ace", T.now), // newer, but not running
    ];
    const out = sortSessions(rows, "project");
    expect(out.map((r) => r.project)).toEqual(["ace", "ace", "ace", "reef"]);
    expect(out[0].running).toBe(true);
    expect(out[1].last_activity_at).toBe(T.now); // then newest non-running
    expect(out[2].last_activity_at).toBe(T.wk1);
  });

  it("sorts web chats (no project) last, not first", () => {
    const rows = [row("", T.now), row("ace", T.wk1)];
    expect(sortSessions(rows, "project").map((r) => r.project)).toEqual(["ace", ""]);
  });
});

describe("projectHeader", () => {
  it("labels the first row of each project run and nothing else", () => {
    const rows = sortSessions(
      [row("ace", T.now), row("ace", T.hr4), row("reef", T.hr4)],
      "project",
    );
    expect(rows.map((_, i) => projectHeader(rows, i, "project"))).toEqual([
      "ace",
      null,
      "reef",
    ]);
  });

  it("never labels in time mode", () => {
    const rows = [row("ace", T.now), row("reef", T.hr4)];
    expect(rows.map((_, i) => projectHeader(rows, i, "time"))).toEqual([null, null]);
  });

  it("labels a blank project readably", () => {
    const rows = [row("", T.now)];
    expect(projectHeader(rows, 0, "project")).toBe("No project");
  });
});

describe("a session waiting on a human", () => {
  const waiting = (project: string, at: string) => ({
    ...row(project, at),
    waiting_on_you: true,
  });

  it("sorts above everything, however long ago it last spoke", () => {
    // The trap this exists for: a session stops writing the INSTANT it asks, so
    // activity ordering pushes it further down the longer it is kept waiting —
    // the one row you can act on ends up under every row you cannot.
    const rows = [
      row("canopy-web", T.now, true),
      row("ace", T.min5),
      waiting("spark", T.wk1),
    ];
    expect(sortSessions(rows, "time").map((r) => r.project)).toEqual([
      "spark",
      "canopy-web",
      "ace",
    ]);
  });

  it("stays inside its project group in project mode", () => {
    // Project mode's contract is that a row appears under its repo heading;
    // floating a waiting row out of its group would break the grouping it is
    // named for.
    const rows = [
      row("ace", T.now),
      waiting("reef", T.wk1),
      row("ace", T.wk1),
    ];
    expect(sortSessions(rows, "project").map((r) => r.project)).toEqual([
      "ace",
      "ace",
      "reef",
    ]);
  });

  it("outranks running, which is not the same question", () => {
    // "running" means the agent is busy; "waiting" means YOU are the blocker.
    const rows = [row("busy", T.now, true), waiting("blocked", T.hr4)];
    expect(sortSessions(rows, "time")[0].project).toBe("blocked");
  });
});
