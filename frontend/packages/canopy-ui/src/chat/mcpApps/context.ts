import { createContext, useContext } from "react";

import type { CallToolResult } from "./bridge";

/** What the server put on a tool result row that has an MCP Apps View
 *  (`MessageOut.app`; spec 2026-10-08 §3). Decided server-side — the client
 *  never guesses which tools have Views. */
export interface AppRef {
  tool_call_id: string;
  site: string;
  tool: string;
  resource_uri: string;
  path?: string;
}

export interface AppReceipt {
  tool: string;
  by: { name: string };
  at: string;
  is_error: boolean;
  result?: string;
}

/** `GET …/apps/{tool_call_id}/resource` — the View, read as the person looking. */
export interface AppViewResource {
  tool_call_id: string;
  site: string;
  tool: string;
  html: string;
  csp: Record<string, string[]>;
  sandbox_src: string;
  prefers_border: boolean | null;
  can_act: boolean;
  read_only_reason: string;
  sign_in_url: string;
  tool_input: Record<string, unknown>;
  tool_result: CallToolResult | null;
  receipts: AppReceipt[];
}

/** How a host page reaches canopy for one session's Views. ChatPage wires it
 *  to `/api/canopy-sessions/{id}/apps/…`, the embed panel to the same or (for a
 *  contact) `/api/contact/sessions/{id}/apps/…`. Absent, no View renders and a
 *  tool result looks exactly as it always did (ace-web, older hosts). */
export interface AppHost {
  load(toolCallId: string): Promise<AppViewResource>;
  callTool(toolCallId: string, name: string, args: Record<string, unknown>): Promise<CallToolResult>;
  readResource(toolCallId: string, uri: string): Promise<unknown>;
  updateContext(toolCallId: string, params: Record<string, unknown>): Promise<void>;
  sendMessage(toolCallId: string, text: string): Promise<void>;
  /** A View's `resources/subscribe` / `resources/unsubscribe`, as the viewer.
   *  Absent, the View is told the host offers no subscriptions. */
  subscribe?(toolCallId: string, uri: string): Promise<void>;
  unsubscribe?(toolCallId: string, uri: string): Promise<void>;
  /** The host's `notifications/resources/updated`, as canopy relays them over
   *  the session socket (`app.resource_updated`). Returns the unsubscriber. */
  onResourceUpdated?(listener: AppResourceListener): () => void;
  /** Turns the server's `sandbox_src` into a frame URL (a path prefix, an origin). */
  resolveUrl?(path: string): string;
  /** The origin the proxy's messages carry; "null" (opaque) unless the sandbox
   *  moved to a dedicated origin. */
  proxyOrigin?: string;
  /** Narrow panels (the embed) pass their width. */
  maxWidth?: number;
  /** The tallest a View may grow inline, in px (default 900). */
  maxHeight?: number;
  /** Whether a View may go full screen (`ui/request-display-mode`): drawn over
   *  the surface with a close control. Absent, every View stays inline. */
  fullscreen?: boolean;
}

export type AppResourceListener = (toolCallId: string, uri: string) => void;

/** Fans `app.resource_updated` socket frames out to the Views listening.
 *  A host page keeps one, calls `emit` from its socket's `onUnknownEvent`, and
 *  hands `onResourceUpdated` to its `AppHost`. */
export interface AppResourceUpdates {
  emit(toolCallId: string, uri: string): void;
  onResourceUpdated(listener: AppResourceListener): () => void;
}

export function createAppResourceUpdates(): AppResourceUpdates {
  const listeners = new Set<AppResourceListener>();
  return {
    emit(toolCallId, uri) {
      for (const listener of [...listeners]) {
        try {
          listener(toolCallId, uri);
        } catch {
          // One View's failure never stops the others hearing.
        }
      }
    },
    onResourceUpdated(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** A socket frame, if it is `app.resource_updated`: `{tool_call_id, uri}`. */
export function appResourceUpdate(frame: { event: string; data?: unknown }):
    { toolCallId: string; uri: string } | null {
  if (frame.event !== "app.resource_updated") return null;
  const data = (frame.data ?? {}) as { tool_call_id?: unknown; uri?: unknown };
  const toolCallId = typeof data.tool_call_id === "string" ? data.tool_call_id : "";
  const uri = typeof data.uri === "string" ? data.uri : "";
  return toolCallId && uri ? { toolCallId, uri } : null;
}

export const AppHostContext = createContext<AppHost | null>(null);

export function useAppHost(): AppHost | null {
  return useContext(AppHostContext);
}
