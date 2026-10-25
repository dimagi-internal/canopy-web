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
    expect(row.textContent).toContain("Summary of earlier messages");
    // A rule across the transcript says where it happened.
    expect(screen.getByTestId("compaction-divider").textContent).toContain(
      "Context compacted — earlier messages summarised",
    );
  });

  it("leaves an ordinary typed line as a bubble", () => {
    render(<MessageItem message={userRow({ record: {} })} />);
    expect(screen.queryByTestId("compaction-summary-row")).toBeNull();
  });
});

function agentRow(plaintext: string, content: Record<string, unknown> = {}): Message {
  return { ...userRow(content), role: "assistant", plaintext };
}

describe("MessageItem — Claude Code's own error notice", () => {
  it("renders a flagged signed-out row as 'didn't run', not as the agent's reply", () => {
    render(<MessageItem message={agentRow("Not logged in · Please run /login",
      { api_error: "api_error", signed_out: true })} />);
    const row = screen.getByTestId("api-error-row");
    expect(row.textContent).toContain("Claude isn't signed in on the runner");
    expect(row.textContent).toContain("Not logged in · Please run /login");
  });

  it("recognises the same line on a row stored before the flag existed", () => {
    render(<MessageItem message={agentRow("Not logged in · Please run /login")} />);
    expect(screen.getByTestId("api-error-row")).toBeTruthy();
  });

  it("leaves an agent talking ABOUT logging in as a reply", () => {
    render(<MessageItem message={agentRow("You're not logged in to GitHub yet, so please run /login after that.")} />);
    expect(screen.queryByTestId("api-error-row")).toBeNull();
  });
});

describe("MessageItem — the files sent with a line", () => {
  it("shows what the host renders under a person's line, and nothing under the agent's", async () => {
    const { AttachmentHostContext } = await import("./attachmentHost");
    const host = { render: (m: Message) => <span data-testid="files">files of {m.turn_index}</span> };
    render(
      <AttachmentHostContext.Provider value={host}>
        <MessageItem message={userRow({})} />
        <MessageItem message={agentRow("a reply")} />
      </AttachmentHostContext.Provider>,
    );
    expect(screen.getAllByTestId("files").map((e) => e.textContent)).toEqual(["files of 1"]);
  });
});
