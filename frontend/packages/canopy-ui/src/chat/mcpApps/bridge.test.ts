// @vitest-environment jsdom
/**
 * The host end of an MCP Apps View's wire (spec 2026-10-08 test plan, frontend):
 * the handshake order, who may speak, what is never forwarded, and what a viewer
 * who may not act is told.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppBridge, type AppBridgeHandlers, type CallToolResult } from "./bridge";

type Sent = Record<string, unknown>;

function setup(opts: { canAct?: boolean; handlers?: Partial<AppBridgeHandlers>;
                        toolResult?: CallToolResult | null } = {}) {
  const iframe = document.createElement("iframe");
  document.body.appendChild(iframe);
  const proxy = iframe.contentWindow as Window;
  const sent: Sent[] = [];
  vi.spyOn(proxy, "postMessage").mockImplementation(((msg: unknown) => {
    sent.push(msg as Sent);
  }) as Window["postMessage"]);
  const handlers: AppBridgeHandlers = {
    callTool: vi.fn(async () => ({ content: [{ type: "text", text: "ok" }], isError: false })),
    readResource: vi.fn(async () => ({ contents: [] })),
    updateModelContext: vi.fn(async () => undefined),
    sendMessage: vi.fn(async () => undefined),
    openLink: vi.fn(),
    onSize: vi.fn(),
    ...opts.handlers,
  };
  const bridge = new AppBridge({
    getProxyWindow: () => proxy,
    html: "<!doctype html><p>view</p>",
    csp: { connectDomains: ["https://labs.test"] },
    canAct: opts.canAct ?? true,
    hostContext: { theme: "light", displayMode: "inline" },
    toolInput: { run_id: 5 },
    toolResult: opts.toolResult !== undefined ? opts.toolResult
      : { content: [{ type: "text", text: "preview" }] },
    handlers,
  });
  const fromProxy = (data: unknown, { source = proxy as MessageEventSource | null, origin = "null" } = {}) =>
    window.dispatchEvent(new MessageEvent("message", { data, source, origin }));
  return { bridge, proxy, sent, handlers, fromProxy };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("AppBridge", () => {
  it("runs the handshake in order and sends nothing to the View before initialized", async () => {
    const { sent, fromProxy } = setup();
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} });
    expect(sent.map((m) => m.method)).toEqual(["ui/notifications/sandbox-resource-ready"]);
    const ready = sent[0].params as Record<string, unknown>;
    expect(ready.html).toContain("<p>view</p>");
    expect(ready.sandbox).toBe("allow-scripts");
    // A second proxy-ready does not re-send the resource.
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} });
    expect(sent).toHaveLength(1);

    fromProxy({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: { appCapabilities: {} } });
    await flush();
    expect(sent).toHaveLength(2);
    expect(sent[1].id).toBe(1);
    expect((sent[1].result as Record<string, unknown>).protocolVersion).toBe("2026-01-26");
    // Still nothing UNSOLICITED: the tool data waits for `initialized`.
    expect(sent.some((m) => m.method === "ui/notifications/tool-input")).toBe(false);

    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    expect(sent.slice(2).map((m) => m.method)).toEqual([
      "ui/notifications/tool-input",
      "ui/notifications/tool-result",
    ]);
    expect(sent[2].params).toEqual({ arguments: { run_id: 5 } });
  });

  it("drops messages from any other source, a non-null origin, or that are not JSON-RPC", async () => {
    const { sent, fromProxy } = setup();
    const other = document.createElement("iframe");
    document.body.appendChild(other);
    const hello = { jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} };
    fromProxy(hello, { source: other.contentWindow });          // another frame, also "null"
    fromProxy(hello, { origin: "https://evil.test" });           // right frame, wrong origin
    fromProxy({ method: "ui/notifications/sandbox-proxy-ready" }); // not JSON-RPC 2.0
    fromProxy("garbage");
    await flush();
    expect(sent).toEqual([]);
  });

  it("swallows sandbox-* notifications from the View instead of acting on them", async () => {
    const { sent, fromProxy } = setup();
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/sandbox-resource-ready",
                params: { html: "<script>evil()</script>" } });
    await flush();
    expect(sent).toEqual([]);
  });

  it("proxies tools/call to canopy and answers with the result", async () => {
    const { sent, handlers, fromProxy } = setup();
    fromProxy({ jsonrpc: "2.0", id: 7, method: "tools/call",
                params: { name: "workflow_action_preview_view", arguments: { run_id: 5 } } });
    await flush();
    expect(handlers.callTool).toHaveBeenCalledWith("workflow_action_preview_view", { run_id: 5 });
    expect(sent[0]).toMatchObject({ id: 7, result: { isError: false } });
  });

  it("a viewer who may not act gets no serverTools and every act refused", async () => {
    const { bridge, sent, handlers, fromProxy } = setup({ canAct: false });
    expect(bridge.hostCapabilities()).not.toHaveProperty("serverTools");
    fromProxy({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: {} });
    await flush();
    expect((sent[0].result as { hostCapabilities: object }).hostCapabilities)
      .not.toHaveProperty("serverTools");
    for (const [i, method] of ["tools/call", "resources/read", "ui/message",
                               "ui/update-model-context"].entries()) {
      fromProxy({ jsonrpc: "2.0", id: 10 + i, method,
                  params: { name: "x", uri: "ui://x", content: { type: "text", text: "hi" } } });
    }
    await flush();
    expect(handlers.callTool).not.toHaveBeenCalled();
    expect(handlers.sendMessage).not.toHaveBeenCalled();
    expect(sent.slice(1).every((m) => m.error)).toBe(true);
  });

  it("ui/open-link accepts https only", async () => {
    const { sent, handlers, fromProxy } = setup();
    fromProxy({ jsonrpc: "2.0", id: 1, method: "ui/open-link", params: { url: "javascript:alert(1)" } });
    fromProxy({ jsonrpc: "2.0", id: 2, method: "ui/open-link", params: { url: "http://labs.test" } });
    fromProxy({ jsonrpc: "2.0", id: 3, method: "ui/open-link", params: { url: "https://labs.test/run/5" } });
    await flush();
    expect(handlers.openLink).toHaveBeenCalledTimes(1);
    expect(handlers.openLink).toHaveBeenCalledWith("https://labs.test/run/5");
    expect(sent.filter((m) => m.error).map((m) => m.id)).toEqual([1, 2]);
  });

  it("answers display-mode, ping, size and unknown methods per the spec", async () => {
    const { sent, handlers, fromProxy } = setup();
    fromProxy({ jsonrpc: "2.0", id: 1, method: "ui/request-display-mode", params: { mode: "fullscreen" } });
    fromProxy({ jsonrpc: "2.0", id: 2, method: "ping" });
    fromProxy({ jsonrpc: "2.0", id: 3, method: "sampling/createMessage", params: {} });
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { width: 300, height: 420 } });
    await flush();
    expect(sent[0]).toMatchObject({ id: 1, result: { mode: "inline" } });
    expect(sent[1]).toMatchObject({ id: 2, result: {} });
    expect(sent[2]).toMatchObject({ id: 3, error: { code: -32601 } });
    expect(handlers.onSize).toHaveBeenCalledWith({ width: 300, height: 420 });
  });

  it("asks the host for a display mode when it offers one, and answers what is in force", async () => {
    const requestDisplayMode = vi.fn(async (mode: string) => (mode === "fullscreen" ? "fullscreen" : "inline"));
    const { sent, fromProxy } = setup({ handlers: { requestDisplayMode } });
    fromProxy({ jsonrpc: "2.0", id: 1, method: "ui/request-display-mode", params: { mode: "fullscreen" } });
    fromProxy({ jsonrpc: "2.0", id: 2, method: "ui/request-display-mode", params: { mode: "pip" } });
    await flush();
    expect(requestDisplayMode).toHaveBeenCalledWith("fullscreen");
    expect(sent.find((m) => m.id === 1)).toMatchObject({ result: { mode: "fullscreen" } });
    expect(sent.find((m) => m.id === 2)).toMatchObject({ result: { mode: "inline" } });
  });

  it("tells an initialized View its host context changed, and nothing before", async () => {
    const { bridge, sent, fromProxy } = setup();
    bridge.notifyHostContext({ displayMode: "fullscreen" });
    expect(sent).toHaveLength(0);
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    bridge.notifyHostContext({ displayMode: "fullscreen" });
    expect(sent.at(-1)).toMatchObject({ method: "ui/notifications/host-context-changed",
                                         params: { displayMode: "fullscreen" } });
  });

  it("ui/message and update-model-context reach canopy", async () => {
    const { handlers, fromProxy } = setup();
    fromProxy({ jsonrpc: "2.0", id: 1, method: "ui/message",
                params: { role: "user", content: { type: "text", text: "Sent" } } });
    fromProxy({ jsonrpc: "2.0", id: 2, method: "ui/update-model-context",
                params: { structuredContent: { outcome: "declined" } } });
    await flush();
    expect(handlers.sendMessage).toHaveBeenCalledWith("Sent");
    expect(handlers.updateModelContext).toHaveBeenCalledWith({ structuredContent: { outcome: "declined" } });
  });

  it("stops listening when closed", async () => {
    const { bridge, sent, fromProxy } = setup();
    bridge.close();
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} });
    await flush();
    expect(sent).toEqual([]);
  });

  it("hands a sensitive conversation's links-only result to the View intact", async () => {
    const withheld: CallToolResult = {
      content: [{ type: "resource_link", uri: "labs-result://r1", name: "Visits",
                  mimeType: "application/json", description: "12 rows" }],
      structuredContent: null,
      _meta: { "canopy/withheld": true },
    };
    const { sent, fromProxy } = setup({ toolResult: withheld });
    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    const out = sent.find((m) => m.method === "ui/notifications/tool-result");
    expect(out?.params).toEqual(withheld);
  });

  it("subscribes and unsubscribes through the host, and relays updates only for subscribed URIs", async () => {
    const subscribeResource = vi.fn(async () => undefined);
    const unsubscribeResource = vi.fn(async () => undefined);
    const { bridge, sent, fromProxy } = setup({ handlers: { subscribeResource, unsubscribeResource } });
    expect(bridge.hostCapabilities().serverResources).toEqual({ subscribe: true });
    // Before the View is initialized nothing is relayed, subscribed or not.
    fromProxy({ jsonrpc: "2.0", id: 1, method: "resources/subscribe", params: { uri: "labs-result://r1" } });
    await flush();
    expect(subscribeResource).toHaveBeenCalledWith("labs-result://r1");
    expect(sent.find((m) => m.id === 1)).toMatchObject({ result: {} });
    bridge.notifyResourceUpdated("labs-result://r1");
    expect(sent.some((m) => m.method === "notifications/resources/updated")).toBe(false);

    fromProxy({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    bridge.notifyResourceUpdated("labs-result://other");
    bridge.notifyResourceUpdated("labs-result://r1");
    const updates = sent.filter((m) => m.method === "notifications/resources/updated");
    expect(updates).toEqual([{ jsonrpc: "2.0", method: "notifications/resources/updated",
                               params: { uri: "labs-result://r1" } }]);

    fromProxy({ jsonrpc: "2.0", id: 2, method: "resources/unsubscribe", params: { uri: "labs-result://r1" } });
    await flush();
    expect(unsubscribeResource).toHaveBeenCalledWith("labs-result://r1");
    expect(bridge.subscriptions()).toEqual([]);
    bridge.notifyResourceUpdated("labs-result://r1");
    expect(sent.filter((m) => m.method === "notifications/resources/updated")).toHaveLength(1);
  });

  it("refuses subscriptions to a viewer who may not act, and without a host handler", async () => {
    const subscribeResource = vi.fn(async () => undefined);
    const readOnly = setup({ canAct: false, handlers: { subscribeResource } });
    readOnly.fromProxy({ jsonrpc: "2.0", id: 3, method: "resources/subscribe", params: { uri: "labs-result://r1" } });
    await flush();
    expect(subscribeResource).not.toHaveBeenCalled();
    expect(readOnly.sent.find((m) => m.id === 3)).toMatchObject({ error: { code: -32000 } });
    expect(readOnly.bridge.hostCapabilities().serverResources).toBeUndefined();
    readOnly.bridge.close();

    const none = setup();
    expect(none.bridge.hostCapabilities().serverResources).toEqual({});
    none.fromProxy({ jsonrpc: "2.0", id: 4, method: "resources/subscribe", params: { uri: "labs-result://r1" } });
    await flush();
    expect(none.sent.find((m) => m.id === 4)).toMatchObject({ error: { code: -32601 } });
  });
});
