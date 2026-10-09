/**
 * The HOST side of an MCP Apps View (SEP-1865, `io.modelcontextprotocol/ui`,
 * spec 2026-01-26 §Communication Protocol, §Sandbox proxy, §Lifecycle).
 *
 * canopy frames a sandbox proxy (`/mcp-apps/sandbox/`, opaque origin); the proxy
 * frames the View and relays JSON-RPC both ways. This module is the host's end
 * of that wire: it answers the View's requests (`ui/initialize`, `tools/call`,
 * `resources/read`, `ui/open-link`, `ui/message`, `ui/update-model-context`,
 * `ui/request-display-mode`, `ping`) through callbacks that go to canopy's REST
 * routes, and sends the View its tool input and result.
 *
 * Why not `@modelcontextprotocol/ext-apps/app-bridge`: it peer-depends on
 * `@modelcontextprotocol/{client,core,server}` v2 and zod 4, and this file ships
 * inside `canopy-ui`, a published package whose consumers (ace-web) would all
 * inherit those peers for one component. The wire is small and fully specified;
 * this follows it method for method.
 *
 * **Who may talk.** The proxy's origin is OPAQUE (owner decision 1), so every
 * message from it says `origin: "null"` — which is also what any other sandboxed
 * frame on the page would say. The check is therefore the SOURCE: a message is
 * accepted only when `event.source` is THIS proxy frame's `contentWindow`, and
 * its origin is `"null"` (or the configured sandbox origin, if it ever moves to
 * one). Anything else, or anything that is not JSON-RPC 2.0, is dropped.
 *
 * **Order.** Nothing is sent to the View before it says
 * `ui/notifications/initialized` (§Sandbox proxy 6) — then `tool-input`, then
 * `tool-result`, exactly once each.
 */

export const PROTOCOL_VERSION = "2026-01-26";

export interface CallToolResult {
  content: unknown[];
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
  [key: string]: unknown;
}

export interface AppBridgeHandlers {
  callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult>;
  readResource(uri: string): Promise<unknown>;
  updateModelContext(params: Record<string, unknown>): Promise<void>;
  sendMessage(text: string): Promise<void>;
  openLink(url: string): void;
  onSize?(size: { width?: number; height?: number }): void;
  onLog?(params: unknown): void;
}

export interface AppBridgeOptions {
  /** The proxy frame's window. Messages from anything else are ignored. */
  getProxyWindow(): Window | null;
  /** The origin the proxy's messages carry: "null" for the opaque default. */
  proxyOrigin?: string;
  /** The View's HTML and the sandbox it gets inside the proxy. */
  html: string;
  csp?: Record<string, string[]>;
  /** Whether the viewer may act. False omits `serverTools` from the host's
   *  capabilities — the standard's "cannot proxy tool calls" signal, so the
   *  View renders its buttons disabled — and refuses the calls besides. */
  canAct: boolean;
  hostContext: Record<string, unknown>;
  toolInput: Record<string, unknown>;
  toolResult: CallToolResult | null;
  handlers: AppBridgeHandlers;
  hostInfo?: { name: string; version: string };
  /** Defaults to `window`; tests pass their own. */
  target?: Window;
}

type Rpc = {
  jsonrpc: "2.0";
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: unknown;
};

function isRpc(d: unknown): d is Rpc {
  return !!d && typeof d === "object" && (d as Rpc).jsonrpc === "2.0";
}

export class AppBridge {
  private readonly opts: AppBridgeOptions;
  private readonly target: Window;
  private readonly listener: (ev: MessageEvent) => void;
  private resourceSent = false;
  private initialized = false;
  private dataSent = false;
  private nextId = 1;
  private closed = false;

  constructor(opts: AppBridgeOptions) {
    this.opts = opts;
    this.target = opts.target ?? window;
    this.listener = (ev) => this.onMessage(ev);
    this.target.addEventListener("message", this.listener);
  }

  /** The host capabilities the View sees in its `ui/initialize` result. */
  hostCapabilities(): Record<string, unknown> {
    const caps: Record<string, unknown> = {
      openLinks: {},
      logging: {},
      sandbox: { csp: this.opts.csp ?? {}, permissions: {} },
    };
    if (this.opts.canAct) {
      caps.serverTools = {};
      caps.serverResources = {};
    }
    return caps;
  }

  /** §Lifecycle 4: tell the View before tearing it down. Best effort. */
  close(reason = "unmounted"): void {
    if (this.closed) return;
    if (this.initialized) {
      this.post({ jsonrpc: "2.0", id: `teardown-${this.nextId++}`, method: "ui/resource-teardown",
                  params: { reason } });
    }
    this.closed = true;
    this.target.removeEventListener("message", this.listener);
  }

  private post(msg: Rpc): void {
    const win = this.opts.getProxyWindow();
    // The proxy's origin is opaque, so "*" is the only targetOrigin that
    // reaches it; WHICH window receives it is fixed by `win`, not by origin.
    win?.postMessage(msg, "*");
  }

  private accepted(ev: MessageEvent): boolean {
    const win = this.opts.getProxyWindow();
    if (!win || ev.source !== win) return false;
    return ev.origin === (this.opts.proxyOrigin ?? "null");
  }

  private onMessage(ev: MessageEvent): void {
    if (this.closed || !this.accepted(ev) || !isRpc(ev.data)) return;
    const msg = ev.data;
    const method = typeof msg.method === "string" ? msg.method : null;
    if (method === null) return; // a response to our teardown; nothing to do
    if (method === "ui/notifications/sandbox-proxy-ready") {
      if (this.resourceSent) return;
      this.resourceSent = true;
      this.post({
        jsonrpc: "2.0",
        method: "ui/notifications/sandbox-resource-ready",
        params: { html: this.opts.html, csp: this.opts.csp ?? {}, sandbox: "allow-scripts",
                  permissions: {} },
      });
      return;
    }
    if (method.startsWith("ui/notifications/sandbox-")) return;
    if (msg.id === undefined || msg.id === null) {
      this.onNotification(method, msg.params ?? {});
      return;
    }
    void this.onRequest(msg.id, method, msg.params ?? {});
  }

  private onNotification(method: string, params: Record<string, unknown>): void {
    if (method === "ui/notifications/initialized") {
      if (this.initialized) return;
      this.initialized = true;
      this.sendToolData();
      return;
    }
    if (method === "ui/notifications/size-changed") {
      const width = typeof params.width === "number" ? params.width : undefined;
      const height = typeof params.height === "number" ? params.height : undefined;
      this.opts.handlers.onSize?.({ width, height });
      return;
    }
    if (method === "notifications/message") {
      this.opts.handlers.onLog?.(params);
    }
  }

  private sendToolData(): void {
    if (this.dataSent) return;
    this.dataSent = true;
    this.post({ jsonrpc: "2.0", method: "ui/notifications/tool-input",
                params: { arguments: this.opts.toolInput } });
    if (this.opts.toolResult) {
      this.post({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                  params: this.opts.toolResult as unknown as Record<string, unknown> });
    }
  }

  private respond(id: string | number, result: unknown): void {
    this.post({ jsonrpc: "2.0", id, result });
  }

  private fail(id: string | number, code: number, message: string): void {
    this.post({ jsonrpc: "2.0", id, error: { code, message } });
  }

  private async onRequest(id: string | number, method: string,
                          params: Record<string, unknown>): Promise<void> {
    const h = this.opts.handlers;
    try {
      switch (method) {
        case "ui/initialize":
          this.respond(id, {
            protocolVersion: PROTOCOL_VERSION,
            hostCapabilities: this.hostCapabilities(),
            hostInfo: this.opts.hostInfo ?? { name: "canopy", version: "1" },
            hostContext: this.opts.hostContext,
          });
          return;
        case "ping":
          this.respond(id, {});
          return;
        case "ui/request-display-mode":
          // Inline only (spec 2026-10-08 §7): the answer is the mode in force.
          this.respond(id, { mode: "inline" });
          return;
        case "ui/open-link": {
          const url = typeof params.url === "string" ? params.url : "";
          if (!isHttps(url)) {
            this.fail(id, -32000, "Invalid URL");
            return;
          }
          h.openLink(url);
          this.respond(id, {});
          return;
        }
        case "tools/call": {
          if (!this.opts.canAct) {
            this.fail(id, -32000, "You can't act from this view");
            return;
          }
          const name = typeof params.name === "string" ? params.name : "";
          const args = (params.arguments && typeof params.arguments === "object")
            ? params.arguments as Record<string, unknown> : {};
          this.respond(id, await h.callTool(name, args));
          return;
        }
        case "resources/read": {
          if (!this.opts.canAct) {
            this.fail(id, -32000, "You can't act from this view");
            return;
          }
          const uri = typeof params.uri === "string" ? params.uri : "";
          this.respond(id, await h.readResource(uri));
          return;
        }
        case "ui/update-model-context":
          if (!this.opts.canAct) {
            this.fail(id, -32000, "Context update denied");
            return;
          }
          await h.updateModelContext(params);
          this.respond(id, {});
          return;
        case "ui/message": {
          if (!this.opts.canAct) {
            this.fail(id, -32000, "Message sending denied");
            return;
          }
          const content = params.content as { type?: string; text?: unknown } | undefined;
          const text = content && content.type === "text" && typeof content.text === "string"
            ? content.text : "";
          if (!text.trim()) {
            this.fail(id, -32000, "Invalid message format");
            return;
          }
          await h.sendMessage(text);
          this.respond(id, {});
          return;
        }
        default:
          this.fail(id, -32601, `Method not found: ${method}`);
      }
    } catch (err) {
      this.fail(id, -32000, err instanceof Error ? err.message : String(err));
    }
  }
}

export function isHttps(url: string): boolean {
  try {
    return new URL(url).protocol === "https:";
  } catch {
    return false;
  }
}
