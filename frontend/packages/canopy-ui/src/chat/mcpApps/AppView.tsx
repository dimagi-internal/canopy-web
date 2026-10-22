import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ExternalLink, Loader2, Minimize2, X } from "lucide-react";

import { AppBridge } from "./bridge";
import { useAppHost, type AppReceipt, type AppRef, type AppViewResource } from "./context";

const MIN_HEIGHT = 80;
const MAX_HEIGHT = 900;

function dimensions(host: { maxWidth?: number; maxHeight?: number }): Record<string, number> {
  return {
    maxHeight: host.maxHeight ?? MAX_HEIGHT,
    ...(host.maxWidth ? { maxWidth: host.maxWidth } : {}),
  };
}

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
 *
 * A host that sets `fullscreen` lets a View ask (`ui/request-display-mode`) to
 * be drawn over the whole surface, with a close control and Esc to come back.
 * The SAME frame is restyled, never remounted: a remount would reload the View
 * and lose whatever its person had done in it. The sandbox is unchanged.
 */
export function AppView({ app }: { app: AppRef }) {
  const host = useAppHost();
  const frame = useRef<HTMLIFrameElement | null>(null);
  const [res, setRes] = useState<AppViewResource | null>(null);
  const [error, setError] = useState<string>("");
  // What the View asked for; clamped to the host's limit at render, so a panel
  // that grows (the embed, expanded) lets an already-tall View use the room.
  const [requested, setRequested] = useState<number>(160);
  const [receipts, setReceipts] = useState<AppReceipt[]>([]);
  const [fullscreen, setFullscreen] = useState(false);
  const fullscreenRef = useRef(false);
  fullscreenRef.current = fullscreen;
  // The host's identity may change when only its SIZE does (the embed panel
  // expanding). Read through a ref, so that never re-loads the View or tears
  // its bridge down — a torn-down bridge would leave the frame talking to nobody.
  const hostRef = useRef(host);
  hostRef.current = host;
  const hasHost = !!host;
  const bridgeRef = useRef<AppBridge | null>(null);

  useEffect(() => {
    const host = hostRef.current;
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
  }, [hasHost, app.tool_call_id]);

  const src = useMemo(() => {
    if (!res) return "";
    const h = hostRef.current;
    return h?.resolveUrl ? h.resolveUrl(res.sandbox_src) : res.sandbox_src;
  }, [res]);

  useEffect(() => {
    const host = hostRef.current;
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
        availableDisplayModes: host.fullscreen ? ["inline", "fullscreen"] : ["inline"],
        platform: "web",
        containerDimensions: dimensions(host),
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
          if (typeof h === "number" && h > 0) setRequested(Math.ceil(h));
        },
        requestDisplayMode: (mode) => {
          const allowed = !!hostRef.current?.fullscreen;
          const next = mode === "fullscreen" && allowed ? "fullscreen"
            : mode === "inline" ? "inline"
            : fullscreenRef.current ? "fullscreen" : "inline";
          setFullscreen(next === "fullscreen");
          bridgeRef.current?.notifyHostContext({ displayMode: next });
          return next;
        },
      },
    });
    bridgeRef.current = bridge;
    return () => {
      bridgeRef.current = null;
      bridge.close();
    };
  }, [res, app.tool_call_id]);

  // A grown or shrunk surface is news to the View: it may lay out for it.
  const maxW = host?.maxWidth;
  const maxH = host?.maxHeight;
  useEffect(() => {
    bridgeRef.current?.notifyHostContext({ containerDimensions: dimensions({ maxWidth: maxW, maxHeight: maxH }) });
  }, [maxW, maxH]);

  // Esc leaves full screen — first, in the capture phase, and marked handled so
  // a surface that also collapses on Esc (the embed panel) waits for the next one.
  useEffect(() => {
    if (!fullscreen) return;
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      exitFullscreen();
    };
    document.addEventListener("keydown", onKeydown, true);
    return () => document.removeEventListener("keydown", onKeydown, true);
  });

  function exitFullscreen(): void {
    setFullscreen(false);
    bridgeRef.current?.notifyHostContext({ displayMode: "inline" });
  }

  if (!host) return null;
  const height = Math.max(MIN_HEIGHT, Math.min(host.maxHeight ?? MAX_HEIGHT, requested));
  const label = siteLabel(app.site);
  const bordered = res?.prefers_border !== false;

  return (
    <section
      data-testid="app-view"
      aria-label={`View from ${label}`}
      data-display-mode={fullscreen ? "fullscreen" : "inline"}
      className={fullscreen
        ? "fixed inset-0 z-50 flex flex-col overflow-hidden bg-background"
        : `my-2 overflow-hidden rounded-lg ${bordered ? "border border-border bg-card" : ""}`}
    >
      <header className="flex items-center gap-2 border-b border-border/60 px-3 py-1.5 text-xs text-muted-foreground">
        <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">from {label}</span>
        <span className="truncate font-mono">{app.tool}</span>
        {fullscreen && (
          <button
            type="button"
            onClick={exitFullscreen}
            aria-label="Exit full screen"
            title="Exit full screen (Esc)"
            className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-foreground hover:bg-muted"
          >
            <Minimize2 className="h-3.5 w-3.5" /> Close
          </button>
        )}
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
            className={fullscreen ? "min-h-0 flex-1" : undefined}
            style={fullscreen
              ? { width: "100%", border: 0, display: "block" }
              : { height, width: "100%", border: 0, display: "block" }}
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
