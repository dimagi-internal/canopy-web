import { describe, expect, it } from "vitest";
import { sessionsPath } from "./chat";

describe("sessionsPath", () => {
  it("omits the param for the default state, so the URL stays the cached one", () => {
    expect(sessionsPath("active")).toBe("/api/me/sessions/");
  });

  it("passes a non-default state through", () => {
    expect(sessionsPath("archived")).toBe("/api/me/sessions/?state=archived");
    expect(sessionsPath("all")).toBe("/api/me/sessions/?state=all");
  });

  it("asks for the last reply only when the feed wants it", () => {
    expect(sessionsPath("active", { reply: true })).toBe("/api/me/sessions/?reply=true");
    expect(sessionsPath("all", { reply: true })).toBe("/api/me/sessions/?state=all&reply=true");
  });

  it("passes a limit through", () => {
    expect(sessionsPath("all", { limit: 500 })).toBe("/api/me/sessions/?state=all&limit=500");
  });
});
