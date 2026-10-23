// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { Message, SessionState, SubagentSummary } from "./protocol";
import { sessionReducer } from "./sessionReducer";
import { SubagentsContext, subagentForRow, subagentProgress } from "./subagents";
import { ToolCallPair } from "./ToolCallPair";

const SUB: SubagentSummary = {
  agent_id: "a1", tool_use_id: "toolu_1", parent_agent_id: "",
  description: "fix the tests", agent_type: "general-purpose", background: true,
  state: "running", steps: 37, last: "Bash · make test",
  started_at: "2026-10-20T10:00:00Z", updated_at: "2026-10-20T10:04:30Z",
};

function row(role: Message["role"], content: Record<string, unknown>, plaintext = ""): Message {
  return {
    id: String(Math.random()), turn_index: 1, role, content, plaintext,
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-20T10:00:00Z",
  };
}

const AGENT_USE = row("tool_use", {
  id: "toolu_1", name: "Agent", input: { description: "fix the tests" },
});
// A background agent's own result comes back the moment it is launched.
const LAUNCHED = row("tool_result", { tool_use_id: "toolu_1" }, "Async agent launched");

afterEach(() => cleanup());

describe("an Agent row with its subagent", () => {
  it("reads running, with its latest step, though its tool call has returned", () => {
    render(
      <SubagentsContext.Provider value={[SUB]}>
        <ToolCallPair use={AGENT_USE} result={LAUNCHED} />
      </SubagentsContext.Provider>,
    );
    const summary = screen.getByText(/running · 37 steps · 4m/);
    expect(summary).toBeTruthy();
    expect(screen.getByText("last: Bash · make test")).toBeTruthy();
  });

  it("drops the latest step once the subagent is done", () => {
    render(
      <SubagentsContext.Provider value={[{ ...SUB, state: "done" }]}>
        <ToolCallPair use={AGENT_USE} result={LAUNCHED} />
      </SubagentsContext.Provider>,
    );
    expect(screen.getByText(/done · 37 steps/)).toBeTruthy();
    expect(screen.queryByText(/^last:/)).toBeNull();
  });

  it("is a plain tool row when no subagent is reported", () => {
    render(<ToolCallPair use={AGENT_USE} result={LAUNCHED} />);
    expect(screen.queryByText(/steps/)).toBeNull();
  });

  it("counts the subagents it started, however deep", () => {
    const child = { ...SUB, agent_id: "b", tool_use_id: "t2", parent_agent_id: "a1" };
    const grandchild = { ...SUB, agent_id: "c", tool_use_id: "t3", parent_agent_id: "b", state: "done" as const };
    const found = subagentForRow(AGENT_USE, [SUB, child, grandchild]);
    expect(found && [found.nested, found.nestedRunning]).toEqual([2, 1]);
  });

  it("matches only an Agent call", () => {
    const bash = row("tool_use", { id: "toolu_1", name: "Bash", input: {} });
    expect(subagentForRow(bash, [SUB])).toBeNull();
  });

  it("says how long in seconds under a minute", () => {
    expect(subagentProgress({ ...SUB, steps: 1, updated_at: "2026-10-20T10:00:12Z" })).toBe("1 step · 12s");
  });
});

describe("session.subagents", () => {
  it("replaces the set wholesale", () => {
    const base = { messages: [], subagents: [SUB] } as unknown as SessionState;
    const next = sessionReducer(base, {
      event: "session.subagents",
      data: { subagents: [{ ...SUB, state: "done" }] },
    });
    expect(next.subagents?.map((s) => s.state)).toEqual(["done"]);
  });
});
