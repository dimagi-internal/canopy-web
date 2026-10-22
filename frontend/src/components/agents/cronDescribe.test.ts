import { describe, it, expect } from "vitest";
import { describeCron } from "./cronDescribe";

describe("describeCron", () => {
  it("renders a weekly schedule", () => {
    expect(describeCron("0 9 * * 5", "America/New_York")).toBe("Fridays at 09:00 · New York");
  });

  it("renders a monthly schedule", () => {
    expect(describeCron("0 9 1 * *", "America/New_York")).toBe(
      "Day 1 monthly at 09:00 · New York",
    );
  });

  it("renders a daily schedule", () => {
    expect(describeCron("30 6 * * *", "UTC")).toBe("Daily at 06:30 · UTC");
  });

  // Every case below is a cron the SERVER accepts (croniter.is_valid → True), so
  // each one is reachable: it saves, then this label renders it. The fallback is
  // always correct if not always pretty; a confidently wrong cadence is not.
  describe("falls back rather than name a cadence wrongly", () => {
    it("falls back on a range in dow", () => {
      expect(describeCron("0 9 * * 1-5", "UTC")).toBe("0 9 * * 1-5 · UTC");
    });

    it("falls back on a macro (does not throw)", () => {
      // Only 1 field — must never blow up on the missing hour/min.
      expect(describeCron("@daily", "UTC")).toBe("@daily · UTC");
    });

    it("falls back on hourly rather than claiming Daily", () => {
      expect(describeCron("0 * * * *", "UTC")).toBe("0 * * * * · UTC");
    });

    it("falls back on a step in minutes", () => {
      expect(describeCron("*/15 * * * *", "UTC")).toBe("*/15 * * * * · UTC");
    });

    it("falls back on a step in minutes at a fixed hour", () => {
      expect(describeCron("*/5 9 * * *", "UTC")).toBe("*/5 9 * * * · UTC");
    });

    it("falls back on a 6-field expression rather than silently dropping a field", () => {
      expect(describeCron("0 0 9 * * 5", "UTC")).toBe("0 0 9 * * 5 · UTC");
    });

    it("falls back on a step in the hour of a weekly shape", () => {
      expect(describeCron("0 */2 * * 5", "UTC")).toBe("0 */2 * * 5 · UTC");
    });

    it("falls back on a list in minutes of a monthly shape", () => {
      expect(describeCron("0,30 9 1 * *", "UTC")).toBe("0,30 9 1 * * · UTC");
    });

    it("is total — never throws for any string", () => {
      for (const s of ["", "   ", "*", "not a cron", "0 9 * *", "0 9 * * * * *"]) {
        expect(() => describeCron(s, "UTC")).not.toThrow();
      }
    });
  });
});
