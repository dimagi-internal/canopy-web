import { describe, expect, it } from "vitest";
import { menuAge } from "./MenuPrompt";
import type { SessionMenu } from "./protocol";

const base: SessionMenu = { question: "Pick one", options: [{ number: 1, label: "A" }] };

describe("menuAge", () => {
  it("says nothing while the dialog is fresh", () => {
    // The runner re-reports every ~10s, so recent lag is normal and naming it
    // would make every menu look doubtful.
    expect(menuAge({ ...base, observed_at: 1_000 }, 1_000_000 + 30_000)).toBe("");
  });

  it("shows an age once nobody has confirmed it lately", () => {
    const now = 1_000_000_000_000;
    expect(menuAge({ ...base, observed_at: now / 1000 - 600 }, now)).toBe("last seen 10m ago");
    expect(menuAge({ ...base, observed_at: now / 1000 - 7200 }, now)).toBe("last seen 2h ago");
  });

  it("says nothing when the producer did not stamp", () => {
    // Pre-`observed_at` rows exist; an unstamped menu must not read as ancient.
    expect(menuAge(base, Date.now())).toBe("");
  });
});
