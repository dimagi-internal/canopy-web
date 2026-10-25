import { useContext, useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronRight, ExternalLink, Loader2, Minimize2, X } from "lucide-react";

import { AppBridge, type CallToolResult } from "./bridge";
import { hostStyleVariables } from "./hostStyles";
import {
  LatestAppViewContext, useAppHost, type AppReceipt, type AppRef, type AppViewResource,
} from "./context";

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

// A person's open/closed choice for one View, kept for the tab (sessionStorage):
// it outlives a reload and a remount from history, never the tab.
const TOGGLE_KEY = "canopy-ui:app-view-open:";

function storedChoice(toolCallId: string): boolean | null {
  try {
    const v = window.sessionStorage.getItem(TOGGLE_KEY + toolCallId);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

function storeChoice(toolCallId: string, open: boolean): void {
  try {
    window.sessionStorage.setItem(TOGGLE_KEY + toolCallId, open ? "1" : "0");
  } catch {
    // No storage (a private window): the choice lasts as long as the mount.
  }
}

type Facts = Record<string, unknown>;

function linkOf(result: CallToolResult | null | undefined): Facts | null {
  for (const c of (result?.content ?? []) as Facts[]) {
    if (c && typeof c === "object" && c.type === "resource_link") return c;
  }
  return null;
}

/** "data_query" → "Data query": a tool name as a person reads it. */
export function humanTool(tool: string): string {
  const words = tool.replace(/^mcp__[^_]+(?:_[^_]+)*?__/, "").split(/[_\s]+/).filter(Boolean);
  if (!words.length) return tool;
  const first = words[0];
  return [first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(" ");
}

/** The View's title: the name its result link carries ("Query over KMC Nigeria"),
 *  else the tool, humanised.
 *
 *  A sensitive conversation keeps the link but not the host's words: canopy stores
 *  it with its URI's scheme as its name (`sensitivity.strip_result`), so that name
 *  ("labs-result") is a placeholder, never a title -- the tool names it instead. */
export function appTitle(result: CallToolResult | null | undefined, tool: string): string {
  const link = linkOf(result);
  const name = typeof link?.name === "string" ? link.name.trim() : "";
  const scheme = typeof link?.uri === "string" ? link.uri.split("://")[0] : "";
  const withheld = Boolean((result?._meta as Facts | undefined)?.["canopy/withheld"]);
  return name && !withheld && name !== scheme ? name.slice(0, 120) : humanTool(tool);
}

/** What a collapsed View says about itself, from its tool result: rows × columns,
 *  the time it took, and whether a row cap cut it -- or, when the conversation
 *  keeps only the result's link (a sensitive one), the link's own description.
 *  MCP Apps has no notification for a View's title or size, so the result is all
 *  a host can read. */
export function appSummary(result: CallToolResult | null | undefined): { text: string; warning: string } {
  const sc = result?.structuredContent;
  const parts: string[] = [];
  let warning = "";
  if (sc && typeof sc === "object") {
    const rows = sc.row_count;
    const cols = Array.isArray(sc.columns) ? sc.columns.length : null;
    if (typeof rows === "number" && Number.isFinite(rows)) {
      parts.push(`${rows.toLocaleString()} row${rows === 1 ? "" : "s"}${cols ? ` × ${cols} column${cols === 1 ? "" : "s"}` : ""}`);
    } else if (typeof sc.title === "string" && sc.title.trim()) {
      parts.push(sc.title.trim().slice(0, 120));
    }
    if (typeof sc.elapsed_ms === "number") parts.push(`${sc.elapsed_ms.toLocaleString()} ms`);
    const cap = sc.row_cap as Facts | undefined;
    if (sc.truncated === true && !(cap && cap.source === "requested")) warning = "cut at the row cap";
  } else {
    const description = linkOf(result)?.description;
    if (typeof description === "string" && description.trim()) parts.push(description.trim().slice(0, 120));
  }
  return { text: parts.join(" · "), warning };
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
 *
 * The header collapses and expands the View. The newest View in the conversation
 * opens expanded and earlier ones collapsed (`LatestAppViewContext`), and a
 * person's own toggle is kept per View for the tab. A View that opens collapsed
 * has no frame until it is first expanded; collapsing one hides its frame
 * without unmounting it, for the same reason full screen never remounts.
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
  // What `load` returns changed (a private conversation's input became readable):
  // load again, into a fresh frame, so the View boots with it.
  const revision = host?.inputRevision;
  const bridgeRef = useRef<AppBridge | null>(null);
  const latest = useContext(LatestAppViewContext);
  const isLatest = latest === null || latest === app.tool_call_id;
  const [choice, setChoice] = useState<boolean | null>(() => storedChoice(app.tool_call_id));
  const expanded = fullscreen || (choice ?? isLatest);
  // Mounted once ever expanded, then kept: a hidden frame keeps its View's state.
  const [framed, setFramed] = useState(expanded);
  if (expanded && !framed) setFramed(true);
  const bodyId = useId();

  function toggle(): void {
    const next = !expanded;
    setChoice(next);
    storeChoice(app.tool_call_id, next);
  }

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let live = true;
    let attempt = 0;
    // A reload starts from no View: the old frame and its bridge go, and the new
    // resource mounts a fresh frame its own bridge binds to.
    setRes(null);
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
  }, [hasHost, app.tool_call_id, revision]);

  const src = useMemo(() => {
    if (!res) return "";
    const h = hostRef.current;
    return h?.resolveUrl ? h.resolveUrl(res.sandbox_src) : res.sandbox_src;
  }, [res]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !res || !framed || !frame.current) return;
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
        // canopy's colours and font, so the View looks like canopy, not its site.
        styles: { variables: hostStyleVariables() },
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
        ...(host.subscribe ? {
          subscribeResource: (uri: string) => host.subscribe!(app.tool_call_id, uri),
          unsubscribeResource: (uri: string) =>
            host.unsubscribe ? host.unsubscribe(app.tool_call_id, uri) : Promise.resolve(),
        } : {}),
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
    // The host's `resources/updated`, relayed by canopy: only this View's call,
    // and the bridge passes on only a URI the View subscribed to.
    const stopListening = host.onResourceUpdated?.((toolCallId, uri) => {
      if (toolCallId === app.tool_call_id) bridge.notifyResourceUpdated(uri);
    });
    return () => {
      stopListening?.();
      // Best effort: a View going away no longer wants to hear about its resources.
      if (host.unsubscribe) {
        for (const uri of bridge.subscriptions()) {
          void host.unsubscribe(app.tool_call_id, uri).catch(() => undefined);
        }
      }
      bridgeRef.current = null;
      bridge.close();
    };
  }, [res, app.tool_call_id, framed]);

  // A grown or shrunk surface is news to the View: it may lay out for it.
  const maxW = host?.maxWidth;
  const maxH = host?.maxHeight;
  useEffect(() => {
    bridgeRef.current?.notifyHostContext({ containerDimensions: dimensions({ maxWidth: maxW, maxHeight: maxH }) });
  }, [maxW, maxH]);

  // canopy's theme switched: the View redraws in the new colours.
  useEffect(() => {
    if (typeof MutationObserver === "undefined" || typeof document === "undefined") return;
    let last = theme();
    const observer = new MutationObserver(() => {
      const now = theme();
      if (now === last) return;
      last = now;
      bridgeRef.current?.notifyHostContext({ theme: now, styles: { variables: hostStyleVariables() } });
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

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
  const title = appTitle(res?.tool_result, app.tool);
  const summary = expanded ? { text: "", warning: "" } : appSummary(res?.tool_result);

  return (
    <section
      data-testid="app-view"
      aria-label={`View from ${label}`}
      data-display-mode={fullscreen ? "fullscreen" : "inline"}
      className={fullscreen
        ? "fixed inset-0 z-50 flex flex-col overflow-hidden bg-background"
        : `my-2 overflow-hidden rounded-lg ${bordered ? "border border-border bg-card" : ""}`}
    >
      <header className={`flex items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground ${expanded ? "border-b border-border/60" : ""}`}>
        {fullscreen ? (
          <span className="flex min-w-0 items-center gap-2" title={app.tool}>
            <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">from {label}</span>
            <span className="truncate font-medium text-foreground">{title}</span>
          </span>
        ) : (
          <button
            type="button"
            onClick={toggle}
            aria-expanded={expanded}
            aria-controls={bodyId}
            title={`${app.tool} · ${expanded ? "collapse" : "expand"}`}
            className="-mx-1 flex min-w-0 flex-1 items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronRight
              aria-hidden
              className={`h-3.5 w-3.5 shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`}
            />
            <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">from {label}</span>
            <span className="truncate font-medium text-foreground">{title}</span>
            {(summary.text || summary.warning) && (
              <span data-testid="app-view-summary" className="truncate">
                {summary.text && <>· {summary.text}</>}
                {summary.warning && <span className="text-warning"> · {summary.warning}</span>}
              </span>
            )}
          </button>
        )}
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
      <div id={bodyId} hidden={!expanded} data-testid="app-view-body" className={fullscreen ? "flex min-h-0 flex-1 flex-col" : undefined}>
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
          {framed && <iframe
            key={String(revision ?? "")}
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
          />}
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
      </div>
    </section>
  );
}
