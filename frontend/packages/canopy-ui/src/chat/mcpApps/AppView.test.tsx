// @vitest-environment jsdom
/**
 * A tool result with an MCP Apps View renders as a sandboxed, labelled frame —
 * in a paired row (ChatPage) and as a standalone row (the embed widget, which
 * never receives the tool_use) — and remounts cleanly from history.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { MessageList } from "../MessageList";
import type { Message } from "../protocol";
import { AppHostContext, createAppResourceUpdates, type AppHost, type AppViewResource } from "./context";

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

describe("AppView full screen", () => {
  async function mounted(over: Partial<AppHost>) {
    const h = { ...host(resource()), ...over } as AppHost;
    render(
      <AppHostContext.Provider value={h}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    const frame = screen.getByTitle("Labs: workflow_run_action") as HTMLIFrameElement;
    const posted: Array<Record<string, unknown>> = [];
    vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(((m: unknown) => {
      posted.push(m as Record<string, unknown>);
    }) as Window["postMessage"]);
    const ask = (mode: string) => act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        data: { jsonrpc: "2.0", id: 7, method: "ui/request-display-mode", params: { mode } },
        source: frame.contentWindow, origin: "null",
      }));
    });
    return { frame, posted, ask, view: () => screen.getByTestId("app-view") };
  }

  it("stays inline when the host does not offer full screen", async () => {
    const { ask, view, posted } = await mounted({});
    await ask("fullscreen");
    await waitFor(() => expect(posted.find((m) => m.id === 7)).toMatchObject({ result: { mode: "inline" } }));
    expect(view().dataset.displayMode).toBe("inline");
  });

  it("goes full screen over the surface, keeps its sandbox, and comes back on close or Esc", async () => {
    const { ask, view, posted, frame } = await mounted({ fullscreen: true });
    await ask("fullscreen");
    await waitFor(() => expect(view().dataset.displayMode).toBe("fullscreen"));
    expect(posted.find((m) => m.id === 7)).toMatchObject({ result: { mode: "fullscreen" } });
    expect(view().className).toContain("fixed");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    // The same frame, restyled — never remounted (that would reload the View).
    expect(screen.getByTitle("Labs: workflow_run_action")).toBe(frame);

    fireEvent.click(screen.getByRole("button", { name: "Exit full screen" }));
    expect(view().dataset.displayMode).toBe("inline");

    await ask("fullscreen");
    await waitFor(() => expect(view().dataset.displayMode).toBe("fullscreen"));
    const esc = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => { document.dispatchEvent(esc); });
    expect(esc.defaultPrevented).toBe(true);
    expect(view().dataset.displayMode).toBe("inline");
  });
});

describe("AppView resource subscriptions", () => {
  it("relays the host's update for its own call to a View that subscribed, and unsubscribes on teardown", async () => {
    const updates = createAppResourceUpdates();
    const subscribe = vi.fn(async () => undefined);
    const unsubscribe = vi.fn(async () => undefined);
    const h = { ...host(resource()), subscribe, unsubscribe,
                onResourceUpdated: updates.onResourceUpdated } as AppHost;
    const tree = render(
      <AppHostContext.Provider value={h}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    const frame = screen.getByTitle("Labs: workflow_run_action") as HTMLIFrameElement;
    const posted: Array<Record<string, unknown>> = [];
    vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(((m: unknown) => {
      posted.push(m as Record<string, unknown>);
    }) as Window["postMessage"]);
    const fromView = (data: unknown) => act(() => {
      window.dispatchEvent(new MessageEvent("message", { data, source: frame.contentWindow, origin: "null" }));
    });
    await fromView({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    await fromView({ jsonrpc: "2.0", id: 9, method: "resources/subscribe", params: { uri: "labs-result://r1" } });
    await waitFor(() => expect(posted.find((m) => m.id === 9)).toMatchObject({ result: {} }));
    expect(subscribe).toHaveBeenCalledWith("toolu_1", "labs-result://r1");

    act(() => {
      updates.emit("toolu_other", "labs-result://r1");
      updates.emit("toolu_1", "labs-result://r1");
    });
    expect(posted.filter((m) => m.method === "notifications/resources/updated")).toEqual([
      { jsonrpc: "2.0", method: "notifications/resources/updated", params: { uri: "labs-result://r1" } },
    ]);

    tree.unmount();
    expect(unsubscribe).toHaveBeenCalledWith("toolu_1", "labs-result://r1");
    updates.emit("toolu_1", "labs-result://r1");
    expect(posted.filter((m) => m.method === "notifications/resources/updated")).toHaveLength(1);
  });
});

describe("AppView inputRevision", () => {
  it("loads the View again, into a fresh frame, when what it loads changes (a private input unlocked)", async () => {
    const res = resource();
    const load = vi.fn(async () => res);
    const base = { ...host(res), load } as AppHost;
    const view = (rev: string) => (
      <AppHostContext.Provider value={{ ...base, inputRevision: rev }}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>
    );
    const { rerender } = render(view("locked"));
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    const before = screen.getByTitle("Labs: workflow_run_action");
    expect(load).toHaveBeenCalledTimes(1);
    load.mockResolvedValue(resource({ tool_input: { q: "Amina" } }));
    rerender(view("open"));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    expect(screen.getByTitle("Labs: workflow_run_action")).not.toBe(before);
    // A host whose size alone changes does not reload it.
    rerender(
      <AppHostContext.Provider value={{ ...base, inputRevision: "open", maxWidth: 400 }}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>,
    );
    expect(load).toHaveBeenCalledTimes(2);
  });
});
