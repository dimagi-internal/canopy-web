// @vitest-environment jsdom
/**
 * A tool result with an MCP Apps View renders as a sandboxed, labelled frame —
 * in a paired row (ChatPage) and as a standalone row (the embed widget, which
 * never receives the tool_use) — and remounts cleanly from history.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { MessageList } from "../MessageList";
import type { Message } from "../protocol";
import { AppHostContext, type AppHost, type AppViewResource } from "./context";

const APP = { tool_call_id: "toolu_1", site: "connect-labs", tool: "workflow_run_action",
              resource_uri: "ui://labs/workflow-action-preview", path: "direct" };

function msg(over: Partial<Message>): Message {
  return { id: "m", turn_index: 1, role: "assistant", content: {}, plaintext: "", status: "complete",
           error_detail: null, started_at: null, completed_at: null, created_at: "", ...over };
}

const use = msg({ id: "u", turn_index: 1, role: "tool_use",
                  content: { id: "toolu_1", name: "mcp__connect_labs__workflow_run_action", input: {} } });
const result = msg({ id: "r", turn_index: 2, role: "tool_result",
                     content: { tool_use_id: "toolu_1", content: "preview" }, app: APP });

function resource(over: Partial<AppViewResource> = {}): AppViewResource {
  return { tool_call_id: "toolu_1", site: "connect-labs", tool: "workflow_run_action",
           html: "<p>view</p>", csp: {}, sandbox_src: "/mcp-apps/sandbox/", prefers_border: true,
           can_act: true, read_only_reason: "", sign_in_url: "", tool_input: {}, tool_result: null,
           receipts: [], ...over };
}

function host(res: AppViewResource): AppHost {
  return {
    load: vi.fn(async () => res),
    callTool: vi.fn(),
    readResource: vi.fn(),
    updateContext: vi.fn(),
    sendMessage: vi.fn(),
    resolveUrl: (p: string) => `/base${p}`,
  } as unknown as AppHost;
}

afterEach(cleanup);

describe("AppView", () => {
  it("renders the View above its tool row, sandboxed with an opaque origin and labelled", async () => {
    const h = host(resource());
    render(
      <AppHostContext.Provider value={h}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    const frame = screen.getByTitle("Labs: workflow_run_action") as HTMLIFrameElement;
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("src")).toBe("/base/mcp-apps/sandbox/");
    expect(screen.getByText("from Labs")).toBeTruthy();
    expect(h.load).toHaveBeenCalledWith("toolu_1");
  });

  it("a standalone View row (the widget) renders the View, not an orphan tool row", async () => {
    render(
      <AppHostContext.Provider value={host(resource())}>
        <MessageList messages={[msg({ ...result, content: { tool_use_id: "toolu_1" } })]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTestId("app-view")).toBeTruthy());
    expect(screen.queryByText("tool_result (orphan)")).toBeNull();
  });

  it("is read-only with a sign-in link for a viewer without a grant, and shows receipts", async () => {
    render(
      <AppHostContext.Provider value={host(resource({
        can_act: false, read_only_reason: "I need you back on the page.",
        sign_in_url: "https://labs.test",
        receipts: [{ tool: "workflow_run_action", by: { name: "Jon" }, at: "2026-10-08T14:02:00Z",
                     is_error: false }],
      }))}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTestId("app-view-readonly")).toBeTruthy());
    expect(screen.getByText(/Sign in to Labs/).closest("a")?.getAttribute("href")).toBe("https://labs.test");
    expect(screen.getByTestId("app-view-receipts").textContent).toContain("workflow_run_action by Jon");
  });

  it("without a host the row is the ordinary tool row (ace-web and older hosts)", () => {
    render(<MessageList messages={[use, result]} />);
    expect(screen.queryByTestId("app-view")).toBeNull();
  });

  it("remounts cleanly from history: a fresh load every mount", async () => {
    const h = host(resource());
    const tree = (
      <AppHostContext.Provider value={h}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>
    );
    const first = render(tree);
    await waitFor(() => expect(screen.getByTestId("app-view")).toBeTruthy());
    first.unmount();
    render(tree);
    await waitFor(() => expect(screen.getByTestId("app-view")).toBeTruthy());
    expect(h.load).toHaveBeenCalledTimes(2);
  });
});
