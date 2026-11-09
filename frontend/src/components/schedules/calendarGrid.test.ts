import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { bucketByDay, matchesFilters } from "./calendarGrid";
import type { ScheduleWeekItem } from "@/api/schedules";

function item(name: string, agent: string, ws: string, fires: string[]): ScheduleWeekItem {
  return {
    // only the fields the helpers read need to be real
    schedule: { name, agent_slug: agent } as ScheduleWeekItem["schedule"],
    workspace_slug: ws,
    fires,
  } as ScheduleWeekItem;
}

describe("bucketByDay", () => {
  it("drops each fire into its local-day column (Mon-indexed)", () => {
    // A week starting Mon 13 July, all constructed from LOCAL date parts (not a
    // UTC-Z literal) so the test's expectations hold under any machine
    // timezone, not just UTC — a hardcoded "...T00:00:00Z" weekStart lands on
    // the PREVIOUS local calendar day in any negative-offset zone (e.g.
    // America/Denver), which is exactly the off-by-one this suite guards
    // against. Fires are serialized to ISO (UTC-on-the-wire) to mirror what
    // the API actually sends, then bucketByDay converts back to local time.
    const weekStart = new Date(2026, 6, 13); // local Mon 13 July 00:00
    const monFire = new Date(2026, 6, 13, 9, 0, 0).toISOString();
    const wedFire = new Date(2026, 6, 15, 9, 0, 0).toISOString();
    const items = [item("Daily", "eva", "alpha", [monFire, wedFire])];
    const cols = bucketByDay(items, weekStart);
    expect(cols).toHaveLength(7);
    expect(cols[0].fires).toHaveLength(1); // Monday
    expect(cols[2].fires).toHaveLength(1); // Wednesday
    expect(cols[1].fires).toHaveLength(0); // Tuesday
    // each bucketed fire carries its source item so a chip can render agent + open the editor
    expect(cols[0].fires[0].item.schedule.name).toBe("Daily");
  });
});

describe("bucketByDay DST regression", () => {
  // This test MUST run under Asia/Jerusalem to exercise the DST math: Israel's
  // spring-forward is a Friday (a 23h local day; here Fri 27 March), MID-WEEK for the
  // week of Monday 23 March. US/EU zones transition on a Sunday (the grid's
  // last column) so the bug is invisible there — a mid-week-transition zone is
  // required. We force TZ deterministically (below) instead of early-returning
  // on the ambient TZ: CI runs under UTC, and the old `if (tz !== 'Asia/
  // Jerusalem') return;` guard made this assert NOTHING in CI — a reviewer could
  // swap the naive fixed-ms division back in and every test still passed.
  //
  // Node re-runs tzset() when process.env.TZ is assigned (vi.stubEnv assigns it),
  // so Dates built after this beforeAll honor the zone (verified: the guard
  // assertion below FAILS if the mechanism ever stops taking effect, rather than
  // silently skipping). unstubAllEnvs restores the ambient TZ afterward.
  beforeAll(() => {
    vi.stubEnv("TZ", "Asia/Jerusalem");
  });
  afterAll(() => {
    vi.unstubAllEnvs();
  });

  it("buckets a just-after-midnight fire on the day after spring-forward into the right column", () => {
    // Guard: prove TZ actually took effect, so a broken mechanism is a RED
    // failure, not a silent skip. Israel is UTC+3 (IDT) after the transition.
    expect(new Date(2026, 2, 28, 0, 30, 0).toString()).toContain("GMT+0300");

    const weekStart = new Date(2026, 2, 23); // local Mon 23 March 00:00
    // A fire in the FIRST local hour after midnight on Saturday 28 March — the
    // day AFTER Friday's lost hour. The naive fixed-ms division floors this into
    // Friday's column (Fri is only 23h, so ~119.5h / 86.4M = 4.98 -> index 4);
    // the DST-safe local-calendar-day index lands it correctly on Saturday (5).
    const satFire = new Date(2026, 2, 28, 0, 30, 0).toISOString();
    const sunFire = new Date(2026, 2, 29, 0, 30, 0).toISOString();
    const items = [item("Weekly", "eva", "alpha", [satFire, sunFire])];
    const cols = bucketByDay(items, weekStart);
    expect(cols[4].fires).toHaveLength(0); // Friday must stay empty
    expect(cols[5].fires).toHaveLength(1); // Saturday = index 5
    expect(cols[6].fires).toHaveLength(1); // Sunday = index 6
  });
});

describe("matchesFilters", () => {
  const it0 = item("A", "eva", "alpha", []);
  it("passes when no filter is set", () => {
    expect(matchesFilters(it0, { agent: null, workspace: null })).toBe(true);
  });
  it("filters by agent", () => {
    expect(matchesFilters(it0, { agent: "eva", workspace: null })).toBe(true);
    expect(matchesFilters(it0, { agent: "hal", workspace: null })).toBe(false);
  });
  it("filters by workspace", () => {
    expect(matchesFilters(it0, { agent: null, workspace: "alpha" })).toBe(true);
    expect(matchesFilters(it0, { agent: null, workspace: "beta" })).toBe(false);
  });
});
