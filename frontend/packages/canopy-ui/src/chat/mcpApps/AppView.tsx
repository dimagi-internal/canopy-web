import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ExternalLink, Loader2, X } from "lucide-react";

import { AppBridge } from "./bridge";
import { useAppHost, type AppReceipt, type AppRef, type AppViewResource } from "./context";

const MIN_HEIGHT = 80;
const MAX_HEIGHT = 900;

function siteLabel(site: string): string {
  return /labs/i.test(site) ? "Labs" : site;
}

function theme(): "light" | "dark" {
  if (typeof document === "undefined") return "light";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

function openLink(url: string): void {
  window.open(url, "_blank", "noopener,noreferrer");
}

function receiptTime(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime())
    ? at
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * One MCP Apps View, rendered sandboxed in the chat (spec 2026-10-08 §7-8).
 *
 * The frame is canopy's sandbox proxy, framed `sandbox="allow-scripts"` with NO
 * `allow-same-origin` (owner decision 1): its origin is opaque, so the View it
 * hosts can reach neither canopy's cookies, storage nor DOM. The View is drawn
 * inside a labelled boundary ("from Labs") so nobody mistakes it for canopy's
 * own UI. Mounting re-reads everything from the server, so a reload never
 * replays anything the previous mount held.
 */
export function AppView({ app }: { app: AppRef }) {
  const host = useAppHost();
  const frame = useRef<HTMLIFrameElement | null>(null);
  const [res, setRes] = useState<AppViewResource | null>(null);
  const [error, setError] = useState<string>("");
  const [height, setHeight] = useState<number>(160);
  const [receipts, setReceipts] = useState<AppReceipt[]>([]);

  useEffect(() => {
    if (!host) return;
    let live = true;
    let attempt = 0;
    const load = () => {
      host.load(app.tool_call_id).then(
        (r) => {
          if (!live) return;
          setRes(r);
          setReceipts(r.receipts ?? []);
        },
        (err: unknown) => {
          if (!live) return;
          // A live frame can arrive before its row is stored; give it a moment.
          if (attempt++ < 3) {
            window.setTimeout(load, 800 * attempt);
            return;
          }
          setError(err instanceof Error ? err.message : "This view could not be loaded.");
        },
      );
    };
    load();
    return () => {
      live = false;
    };
  }, [host, app.tool_call_id]);

  const src = useMemo(() => {
    if (!res) return "";
    return host?.resolveUrl ? host.resolveUrl(res.sandbox_src) : res.sandbox_src;
  }, [host, res]);

  useEffect(() => {
    if (!host || !res || !frame.current) return;
    const refreshReceipts = () =>
      host.load(app.tool_call_id).then((r) => setReceipts(r.receipts ?? []), () => undefined);
    const bridge = new AppBridge({
      getProxyWindow: () => frame.current?.contentWindow ?? null,
      proxyOrigin: host.proxyOrigin,
      html: res.html,
      csp: res.csp,
      canAct: res.can_act,
      toolInput: res.tool_input ?? {},
      toolResult: res.tool_result,
      hostInfo: { name: "canopy", version: "1" },
      hostContext: {
        theme: theme(),
        displayMode: "inline",
        availableDisplayModes: ["inline"],
        platform: "web",
        containerDimensions: { maxHeight: MAX_HEIGHT, ...(host.maxWidth ? { maxWidth: host.maxWidth } : {}) },
        locale: typeof navigator !== "undefined" ? navigator.language : "en",
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        toolInfo: { tool: { name: res.tool, inputSchema: { type: "object" } } },
      },
      handlers: {
        callTool: async (name, args) => {
          const out = await host.callTool(app.tool_call_id, name, args);
          void refreshReceipts();
          return out;
        },
        readResource: (uri) => host.readResource(app.tool_call_id, uri),
        updateModelContext: (params) => host.updateContext(app.tool_call_id, params),
        sendMessage: (text) => host.sendMessage(app.tool_call_id, text),
        openLink,
        onSize: ({ height: h }) => {
          if (typeof h === "number" && h > 0) {
            setHeight(Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, Math.ceil(h))));
          }
        },
      },
    });
    return () => bridge.close();
  }, [host, res, app.tool_call_id]);

  if (!host) return null;
  const label = siteLabel(app.site);
  const bordered = res?.prefers_border !== false;

  return (
    <section
      data-testid="app-view"
      aria-label={`View from ${label}`}
      className={`my-2 overflow-hidden rounded-lg ${bordered ? "border border-border bg-card" : ""}`}
    >
      <header className="flex items-center gap-2 border-b border-border/60 px-3 py-1.5 text-xs text-muted-foreground">
        <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">from {label}</span>
        <span className="truncate font-mono">{app.tool}</span>
      </header>
      {error ? (
        <p className="px-3 py-2 text-sm text-muted-foreground">{error}</p>
      ) : !res ? (
        <p className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading the view…
        </p>
      ) : (
        <>
          {!res.can_act && (
            <p data-testid="app-view-readonly" className="px-3 pt-2 text-xs text-muted-foreground">
              {res.read_only_reason || "This view is read-only for you."}{" "}
              {res.sign_in_url && (
                <a href={res.sign_in_url} target="_blank" rel="noopener noreferrer"
                   className="inline-flex items-center gap-1 font-medium text-primary underline">
                  Sign in to {label} <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </p>
          )}
          <iframe
            ref={frame}
            title={`${label}: ${app.tool}`}
            src={src}
            // Opaque origin: never `allow-same-origin` (owner decision 1).
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            style={{ height, width: "100%", border: 0, display: "block" }}
          />
        </>
      )}
      {receipts.length > 0 && (
        <ul data-testid="app-view-receipts" className="border-t border-border/60 px-3 py-1.5 text-xs text-muted-foreground">
          {receipts.map((r, i) => (
            <li key={`${r.at}-${i}`} className="flex items-center gap-1.5">
              {r.is_error ? <X className="h-3 w-3 text-destructive" /> : <Check className="h-3 w-3 text-success" />}
              <span>
                {r.is_error ? "Failed" : "Done"}: {r.tool} by {r.by?.name || "someone"} at {receiptTime(r.at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
