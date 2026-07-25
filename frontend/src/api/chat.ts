/**
 * Client for the live chat surface (/api/canopy-sessions).
 *
 * Plain fetch (mirrors src/api/sessions.ts) rather than the generated
 * openapi-fetch client — the live transcript's steady-state arrives over the
 * WebSocket (apps/canopy_sessions consumer), not REST. REST covers session
 * meta/create/list, scroll-back paging (`listMessages`), the viewer liveness
 * pair (`attachSession`/`detachSession`), and the runner backfill request
 * (`requestBackfill`). CSRF is attached for mutating calls; response shapes
 * reuse the generated OpenAPI schema so the two can't drift.
 */

import type { components } from "./generated";
import { apiUrl, getCsrfToken } from "./base";

export type ChatSession = components["schemas"]["SessionOut"];
export type ChatSessionDetail = components["schemas"]["SessionDetailOut"];
export type MessagePage = components["schemas"]["MessagePageOut"];
export type StreamState = components["schemas"]["StreamStateOut"];
export type BackfillState = components["schemas"]["BackfillStateOut"];
export type SendResult = components["schemas"]["SendOut"];
export type TurnPlacementResult = components["schemas"]["TurnOutMinimal"];

export class ChatApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = getCsrfToken();
    if (token) headers.set("X-CSRFToken", token);
  }
  const resp = await fetch(apiUrl(path), {
    ...init,
    method,
    headers,
    credentials: "same-origin",
  });
  if (resp.status === 204) return undefined as T;
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // RFC 7807 problem+json: derive a stable code from the type URI tail.
    const type = typeof body.type === "string" ? body.type : "";
    const code = type.split("/").pop() || "error";
    throw new ChatApiError(
      resp.status,
      code,
      body.detail || body.title || "Request failed",
    );
  }
  return body as T;
}

export interface CreateSessionInput {
  title?: string;
  agentSlug?: string;
  // Start an agentless PROJECT chat in this repo (the emdash project name).
  // Mutually exclusive with agentSlug.
  project?: string;
  // Create in this workspace (the chosen agent's OR project's) via the tenant
  // route; omit to use the caller's default.
  workspace?: string;
  metadata?: Record<string, unknown>;
  // Directed placement: pin the session's turns to this runner from creation
  // (rather than the default assignment-cascade routing).
  runnerId?: string;
}

export function createSession(
  input: CreateSessionInput = {},
): Promise<ChatSession> {
  const path = input.workspace
    ? `/api/w/${input.workspace}/canopy-sessions/`
    : "/api/canopy-sessions/";
  return request<ChatSession>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: input.title ?? "",
      agent_slug: input.agentSlug ?? null,
      project: input.project ?? "",
      metadata: input.metadata ?? {},
      runner_id: input.runnerId ?? null,
    }),
  });
}

export function getSession(
  id: string,
  opts: { full?: boolean } = {},
): Promise<ChatSessionDetail> {
  const q = opts.full ? "?full=true" : "";
  return request<ChatSessionDetail>(
    `/api/canopy-sessions/${encodeURIComponent(id)}${q}`,
  );
}

export type SessionState = "active" | "archived" | "all";

/** The list URL for a state. `active` is the server default, so it sends no param. */
export function sessionsPath(state: SessionState = "active"): string {
  return state === "active"
    ? "/api/canopy-sessions/"
    : `/api/canopy-sessions/?state=${state}`;
}

export function listSessions(state: SessionState = "active"): Promise<ChatSession[]> {
  return request<ChatSession[]>(sessionsPath(state));
}

/** One backward page of transcript, for "Load earlier" scroll-back. */
export function listMessages(
  id: string,
  before: number,
  limit?: number,
): Promise<MessagePage> {
  const q = limit != null ? `&limit=${limit}` : "";
  return request<MessagePage>(
    `/api/canopy-sessions/${encodeURIComponent(id)}/messages?before=${before}${q}`,
  );
}

/** Register this viewer as attached (starts live streaming for a bound runner). */
export function attachSession(id: string): Promise<StreamState> {
  return request<StreamState>(`/api/canopy-sessions/${encodeURIComponent(id)}/attach`, {
    method: "POST",
  });
}

/** Detach this viewer (stops streaming once the last viewer leaves). */
export function detachSession(id: string): Promise<StreamState> {
  return request<StreamState>(`/api/canopy-sessions/${encodeURIComponent(id)}/detach`, {
    method: "POST",
  });
}

/** Ask the bound runner to ship the full transcript ("Load full session"). */
export function requestBackfill(id: string): Promise<BackfillState> {
  return request<BackfillState>(`/api/canopy-sessions/${encodeURIComponent(id)}/backfill`, {
    method: "POST",
  });
}

/**
 * Send a message — commits the co-edited draft, enqueuing a session Turn.
 * `placement` is the chat banner's directed-placement decision ("wait" to
 * hold for the pinned runner, "continue" to fall back to the cascade); omit
 * to use the server default.
 */
export function sendMessage(
  id: string,
  text: string,
  clientId = "",
  placement?: string,
): Promise<SendResult> {
  return request<SendResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      client_id: clientId,
      placement: placement ?? null,
    }),
  });
}

/** Re-pin a session's oldest queued turn to a runner (after-the-fact directed placement). */
export function placeTurn(id: string, placement: string): Promise<TurnPlacementResult> {
  return request<TurnPlacementResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/place`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ placement }),
  });
}
