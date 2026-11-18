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

describe("MessageItem — a line no person typed", () => {
  function automatic(plaintext: string, content: Record<string, unknown> = {}): Message {
    return { ...userRow(content), plaintext, automatic: true };
  }

  it("draws an agent's scheduled wake-up on the right, labelled, not as the person's bubble", () => {
    render(<MessageItem message={automatic("PR #2716 is in the merge queue. Check it merged and tell Jonathan.",
      { record: { meta: true, prompt_source: "system" } })} />);
    const row = screen.getByTestId("automatic-row");
    expect(row.className).toContain("ml-auto");
    expect(row.textContent).toContain("not typed by a person");
    expect(screen.queryByTestId("message-row")).toBeNull();
  });

  it("shows a command a trigger ran as the command, not its raw tags", () => {
    render(<MessageItem message={automatic(
      "<command-message>ace:turn</command-message>\n<command-name>/ace:turn</command-name>\n<command-args>--thread 1a1f</command-args>",
    )} />);
    const row = screen.getByTestId("automatic-row");
    expect(row.textContent).toContain("Command");
    expect(row.textContent).toContain("/ace:turn --thread 1a1f");
    expect(row.textContent).not.toContain("<command-name>");
  });

  it("collapses a skill's instructions", () => {
    render(<MessageItem message={automatic("# /ace:turn\n\nProcess ACE's inbound surfaces…", { record: { meta: true } })} />);
    const row = screen.getByTestId("automatic-row");
    expect(row.textContent).toContain("Skill instructions");
    expect(row.hasAttribute("open")).toBe(false);
  });

  it("leaves a line without the server's flag as the person's own bubble", () => {
    render(<MessageItem message={{ ...userRow({ record: { meta: true } }), plaintext: "hello" }} />);
    expect(screen.queryByTestId("automatic-row")).toBeNull();
    expect(screen.getByTestId("message-row")).toBeTruthy();
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
