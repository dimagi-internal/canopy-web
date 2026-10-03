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

  it("asks for the last reply only when the feed wants it", () => {
    expect(sessionsPath("active", { reply: true })).toBe("/api/canopy-sessions/?reply=true");
    expect(sessionsPath("all", { reply: true })).toBe("/api/canopy-sessions/?state=all&reply=true");
  });
});
