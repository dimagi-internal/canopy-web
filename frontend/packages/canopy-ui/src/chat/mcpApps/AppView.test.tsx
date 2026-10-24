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
import { appSummary, appTitle, humanTool } from "./AppView";
import { hostStyleVariables } from "./hostStyles";

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

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

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

describe("AppView collapse", () => {
  const APP2 = { ...APP, tool_call_id: "toolu_2", tool: "data_query" };
  const use2 = msg({ id: "u2", turn_index: 3, role: "tool_use",
                     content: { id: "toolu_2", name: "mcp__connect_labs__data_query", input: {} } });
  const result2 = msg({ id: "r2", turn_index: 4, role: "tool_result",
                        content: { tool_use_id: "toolu_2", content: "rows" }, app: APP2 });

  function twoViews() {
    const h = host(resource());
    (h.load as ReturnType<typeof vi.fn>).mockImplementation(async (id: string) =>
      id === "toolu_1"
        ? resource({ tool_result: { content: [], structuredContent: { row_count: 27, columns: ["a", "b"],
                                                                     elapsed_ms: 412 } } })
        : resource({ tool_call_id: "toolu_2", tool: "data_query" }));
    return (
      <AppHostContext.Provider value={h}>
        <MessageList messages={[use, result, use2, result2]} />
      </AppHostContext.Provider>
    );
  }
  const toggles = () => screen.getAllByRole("button", { expanded: undefined })
    .filter((b) => b.hasAttribute("aria-expanded"));

  it("opens the newest View expanded and earlier ones collapsed, to one line with the row count", async () => {
    render(twoViews());
    await waitFor(() => expect(screen.getByTitle("Labs: data_query")).toBeTruthy());
    const [older, newest] = toggles();
    expect(older.getAttribute("aria-expanded")).toBe("false");
    expect(newest.getAttribute("aria-expanded")).toBe("true");
    // The collapsed View has no frame yet, and says what it holds.
    expect(screen.queryByTitle("Labs: workflow_run_action")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("app-view-summary").textContent)
      .toBe("· 27 rows × 2 columns · 412 ms"));
    // Titled for a person; the tool's own name is the tooltip.
    expect(older.textContent).toContain("Workflow run action");
    expect(older.getAttribute("title")).toContain("workflow_run_action");
    expect(older.textContent).toContain("from Labs");
  });

  it("toggles from the header, keeps the frame when collapsed, and remembers the choice in the tab", async () => {
    const first = render(twoViews());
    await waitFor(() => expect(screen.getByTitle("Labs: data_query")).toBeTruthy());
    const [older, newest] = toggles();

    fireEvent.click(older);
    expect(older.getAttribute("aria-expanded")).toBe("true");
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());

    const frame = screen.getByTitle("Labs: data_query");
    fireEvent.click(newest);
    expect(newest.getAttribute("aria-expanded")).toBe("false");
    // Hidden, not unmounted: the View keeps whatever its person had done in it.
    expect(screen.getByTitle("Labs: data_query")).toBe(frame);
    const body = document.getElementById(newest.getAttribute("aria-controls") ?? "");
    expect(body?.hidden).toBe(true);

    first.unmount();
    render(twoViews());
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    const [olderAgain, newestAgain] = toggles();
    expect(olderAgain.getAttribute("aria-expanded")).toBe("true");
    expect(newestAgain.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTitle("Labs: data_query")).toBeNull();
  });

  it("the toggle is a real button: keyboard reachable, labelled by its header", async () => {
    render(twoViews());
    await waitFor(() => expect(screen.getByTitle("Labs: data_query")).toBeTruthy());
    const [older] = toggles();
    expect(older.tagName).toBe("BUTTON");
    expect(older.getAttribute("type")).toBe("button");
    older.focus();
    expect(document.activeElement).toBe(older);
  });
});

describe("AppView title and summary", () => {
  const link = { type: "resource_link", uri: "labs-result://abc", name: "Query over KMC Nigeria",
                 description: "27 rows, 8 columns" };

  it("titles a View by its result link's name, else by its tool, humanised", () => {
    expect(appTitle({ content: [link] }, "data_query")).toBe("Query over KMC Nigeria");
    expect(appTitle({ content: [] }, "data_query")).toBe("Data query");
    expect(appTitle(null, "workflow_run_action")).toBe("Workflow run action");
    expect(humanTool("mcp__connect_labs__data_query")).toBe("Data query");
  });

  it("summarises rows × columns, the time, and a cut-off only the default cap made", () => {
    const sc = { row_count: 500, columns: ["a", "b", "c"], elapsed_ms: 1200, truncated: true,
                 row_cap: { limit: 500, source: "default" } };
    expect(appSummary({ content: [], structuredContent: sc })).toEqual({
      text: "500 rows × 3 columns · 1,200 ms", warning: "cut at the row cap",
    });
    const asked = { ...sc, row_cap: { limit: 500, source: "requested" } };
    expect(appSummary({ content: [], structuredContent: asked }).warning).toBe("");
  });

  it("a sensitive conversation, which keeps only the link, summarises from the link", () => {
    expect(appSummary({ content: [link], structuredContent: null })).toEqual({
      text: "27 rows, 8 columns", warning: "",
    });
  });

  it("shows the title in the header and a cut-off in the collapsed line", async () => {
    const two = msg({ id: "r2", turn_index: 4, role: "tool_result",
                      content: { tool_use_id: "toolu_2" }, app: { ...APP, tool_call_id: "toolu_2" } });
    const h = host(resource());
    (h.load as ReturnType<typeof vi.fn>).mockImplementation(async (id: string) => resource({
      tool_call_id: id,
      tool_result: { content: [link], structuredContent: { row_count: 500, columns: ["a"], truncated: true } },
    }));
    render(
      <AppHostContext.Provider value={h}>
        <MessageList messages={[result, two]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getAllByText("Query over KMC Nigeria")).toHaveLength(2));
    await waitFor(() => expect(screen.getByTestId("app-view-summary").textContent)
      .toBe("· 500 rows × 1 column · cut at the row cap"));
  });
});

describe("AppView sends canopy's look", () => {
  afterEach(() => {
    for (const name of ["--card", "--primary", "--border", "--muted-foreground", "--font-sans"]) {
      document.documentElement.style.removeProperty(name);
    }
  });

  it("maps canopy's tokens onto the standard MCP Apps style variables", () => {
    const root = document.documentElement.style;
    root.setProperty("--card", "rgb(20, 20, 20)");
    root.setProperty("--primary", "rgb(250, 140, 60)");
    root.setProperty("--border", "rgb(60, 60, 60)");
    root.setProperty("--muted-foreground", "rgb(150, 150, 150)");
    root.setProperty("--font-sans", "'Geist Variable', sans-serif");
    const vars = hostStyleVariables();
    expect(vars["--color-background-primary"]).toBe("rgb(20, 20, 20)");
    expect(vars["--color-text-info"]).toBe("rgb(250, 140, 60)");
    expect(vars["--color-text-secondary"]).toBe("rgb(150, 150, 150)");
    expect(vars["--color-border-primary"]).toBe("rgb(60, 60, 60)");
    expect(vars["--color-background-info"]).toContain("rgb(250, 140, 60)");
    expect(vars["--font-sans"]).toMatch(/^'Geist Variable', sans-serif/);
    expect(vars["--font-mono"]).toBeTruthy();
    // A token the page does not define stays out: the View keeps its own.
    expect("--color-text-danger" in vars).toBe(false);
  });

  it("hands them to the View in hostContext.styles at ui/initialize", async () => {
    document.documentElement.style.setProperty("--primary", "rgb(250, 140, 60)");
    render(
      <AppHostContext.Provider value={host(resource())}>
        <MessageList messages={[use, result]} />
      </AppHostContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTitle("Labs: workflow_run_action")).toBeTruthy());
    const frame = screen.getByTitle("Labs: workflow_run_action") as HTMLIFrameElement;
    const posted: Array<Record<string, unknown>> = [];
    vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(((m: unknown) => {
      posted.push(m as Record<string, unknown>);
    }) as Window["postMessage"]);
    act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        data: { jsonrpc: "2.0", id: 3, method: "ui/initialize", params: {} },
        source: frame.contentWindow, origin: "null",
      }));
    });
    await waitFor(() => expect(posted.find((m) => m.id === 3)).toBeTruthy());
    const answer = posted.find((m) => m.id === 3) as { result: { hostContext: { styles: { variables: Record<string, string> } } } };
    expect(answer.result.hostContext.styles.variables["--color-text-info"]).toBe("rgb(250, 140, 60)");
  });
});
