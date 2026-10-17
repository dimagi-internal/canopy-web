// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { Message } from "./protocol";
import { MessageItem } from "./MessageItem";

function userRow(content: Record<string, unknown>): Message {
  return {
    id: "1", turn_index: 1, role: "user", content,
    plaintext: "This session is being continued from a previous conversation…",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-17T12:35:14Z",
  };
}

afterEach(() => {
  cleanup();
});

describe("MessageItem — a compaction summary", () => {
  it("renders collapsed as context, not as a line the person typed", () => {
    render(<MessageItem message={userRow({ record: { compact_summary: true } })} />);
    const row = screen.getByTestId("compaction-summary-row");
    expect(row.tagName).toBe("DETAILS");
    expect(row.hasAttribute("open")).toBe(false);
    expect(row.textContent).toContain("Context compacted");
  });

  it("leaves an ordinary typed line as a bubble", () => {
    render(<MessageItem message={userRow({ record: {} })} />);
    expect(screen.queryByTestId("compaction-summary-row")).toBeNull();
  });
});
