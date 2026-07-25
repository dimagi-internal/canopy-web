import { describe, expect, it } from "vitest";
import { sessionsPath } from "./chat";

describe("sessionsPath", () => {
  it("omits the param for the default state, so the URL stays the cached one", () => {
    expect(sessionsPath("active")).toBe("/api/canopy-sessions/");
  });

  it("passes a non-default state through", () => {
    expect(sessionsPath("archived")).toBe("/api/canopy-sessions/?state=archived");
    expect(sessionsPath("all")).toBe("/api/canopy-sessions/?state=all");
  });
});
