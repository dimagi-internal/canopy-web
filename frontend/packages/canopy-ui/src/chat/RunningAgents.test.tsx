// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import type { Message, SessionState, SubagentSummary } from "./protocol";

afterEach(cleanup);

function agent(over: Partial<SubagentSummary>): SubagentSummary {
  return {
    agent_id: "a1", tool_use_id: "tu1", parent_agent_id: "", description: "Build consent",
    agent_type: "general-purpose", background: true, state: "running", steps: 54,
    last: "Bash · uv run pytest", started_at: "2026-10-25T10:00:00Z", updated_at: "2026-10-25T10:07:00Z",
    ...over,
  };
}

const agentCall: Message = {
  id: "m1", turn_index: 1, role: "tool_use",
  content: { id: "tu1", name: "Agent", input: { description: "Build consent" } },
  plaintext: "", status: "complete", error_detail: null, started_at: null, completed_at: null, created_at: "",
} as Message;

function state(subagents: SubagentSummary[], messages: Message[] = []): SessionState {
  return { messages, active_draft: null, participants: [], presence_user_ids: [1], current_user_id: 1, subagents } as SessionState;
}

const props = {
  connected: true, currentUserId: 1, onSend: () => undefined, onStop: () => undefined,
  onUpdateDraft: () => undefined, onDiscard: () => undefined,
} as const;

describe("running agents above the composer", () => {
  it("lists each top-level agent still running, with what it is doing", () => {
    render(<ChatPanel {...props} state={state([
      agent({}),
      agent({ agent_id: "a2", tool_use_id: "tu2", description: "Build default agent", steps: 77 }),
      agent({ agent_id: "a3", tool_use_id: "tu3", description: "finished", state: "done" }),
      agent({ agent_id: "a4", tool_use_id: "tu4", description: "nested", parent_agent_id: "a1" }),
    ])} />);
    const rows = screen.getAllByTestId("running-agent").map((r) => r.textContent);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toContain("Build consent");
    expect(rows[0]).toContain("Bash · uv run pytest");
    expect(rows[0]).toContain("54 steps · 7m");
    expect(rows[1]).toContain("Build default agent");
  });

  it("is absent when nothing is running", () => {
    render(<ChatPanel {...props} state={state([agent({ state: "done" })])} />);
    expect(screen.queryByTestId("running-agents")).toBeNull();
  });

  it("scrolls to the agent's own row when clicked", () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    render(<ChatPanel {...props} state={state([agent({})], [agentCall])} />);
    fireEvent.click(screen.getByTestId("running-agent"));
    expect(scroll).toHaveBeenCalled();
    expect((scroll.mock.contexts[0] as Element).getAttribute("data-tool-use-id")).toBe("tu1");
  });
});
