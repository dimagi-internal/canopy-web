// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Message } from "./protocol";
import { ContextMeter } from "./ContextMeter";
import { contextLabel, contextWindow, DEFAULT_WINDOW, LONG_WINDOW, readContext } from "./contextUsage";

afterEach(() => cleanup());

let n = 0;
function row(role: Message["role"], content: Record<string, unknown> = {}): Message {
  n += 1;
  return {
    id: `m${n}`, role, plaintext: "x", content, status: "complete",
    created_at: "2026-10-24T00:00:00Z", turn_index: n,
  } as Message;
}
const used = (tokens: number, model = "claude-opus-5-5") =>
  row("assistant", { usage: { context_tokens: tokens, model } });

describe("the window a call ran in", () => {
  it("is 1M when either model id says [1m]", () => {
    expect(contextWindow(10_000, "claude-opus-5-5[1m]")).toBe(LONG_WINDOW);
    expect(contextWindow(10_000, "claude-opus-5-5", "opus[1m]")).toBe(LONG_WINDOW);
  });
  it("is 1M once the prompt is past 200k, whatever the setting says", () => {
    expect(contextWindow(250_000, "claude-opus-5-5")).toBe(LONG_WINDOW);
  });
  it("is the 200k default when the runner names a model without [1m]", () => {
    expect(contextWindow(10_000, "claude-opus-5-5", "opus")).toBe(DEFAULT_WINDOW);
  });
  it("is unknown when nothing says — the transcript never records [1m]", () => {
    expect(contextWindow(10_000, "claude-opus-5-5")).toBeNull();
    expect(contextWindow(10_000, "claude-opus-5-5", "")).toBeNull();
  });
});

describe("reading the newest rows", () => {
  it("takes the LATEST call and says it as a percentage", () => {
    const r = readContext([used(50_000), row("user"), used(124_000)], "opus");
    expect(r).toMatchObject({ tokens: 124_000, window: DEFAULT_WINDOW, percent: 62 });
    expect(contextLabel(r!)).toBe("62% context");
  });
  it("says the size alone when the window is unknown", () => {
    const r = readContext([used(124_000)], null);
    expect(r?.percent).toBeNull();
    expect(contextLabel(r!)).toBe("124k context");
  });
  it("says unknown, not 0%, when no reply carried usage", () => {
    const r = readContext([row("user"), row("assistant")], "opus");
    expect(contextLabel(r!)).toBe("context unknown");
  });
  it("says nothing for a conversation with no reply yet", () => {
    expect(readContext([row("user")], "opus")).toBeNull();
    expect(readContext([], "opus")).toBeNull();
  });
  it("drops the old number once the conversation compacts after it", () => {
    const summary = row("user", { record: { compact_summary: true } });
    const r = readContext([used(180_000), summary], "opus");
    expect(r).toMatchObject({ tokens: null, compactedSince: true });
    expect(contextLabel(r!)).toBe("context compacted");
    // The next call says the new, smaller number.
    expect(readContext([used(180_000), summary, used(30_000)], "opus")?.percent).toBe(15);
  });
});

describe("<ContextMeter>", () => {
  it("draws a meter with the percentage and the exact numbers on hover", () => {
    render(<ContextMeter messages={[used(124_000)]} runnerModel="opus" />);
    const meter = screen.getByTestId("context-meter");
    expect(meter.textContent).toBe("62% context");
    expect(meter.getAttribute("title")).toContain("124,000 of 200,000 tokens");
    expect(screen.getByRole("meter").getAttribute("aria-valuenow")).toBe("62");
  });
  it("renders nothing before the first reply", () => {
    render(<ContextMeter messages={[row("user")]} runnerModel="opus" />);
    expect(screen.queryByTestId("context-meter")).toBeNull();
  });
});
