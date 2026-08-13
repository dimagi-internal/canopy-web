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

export type ResetResult = components["schemas"]["ResetOut"];

/**
 * Reset a session: drop canopy's derived messages and re-derive them from the
 * runner's transcript. A first-class action, not a repair — these rows are a
 * cache of a file on the runner's disk.
 *
 * Never throws on a refusal: a session with no binding, or whose runner is
 * offline, comes back `ok: false` with a `reason` to render.
 */
export function resetSession(id: string): Promise<ResetResult> {
  return request<ResetResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/reset`, {
    method: "POST",
  });
}

export type CloseResult = { ok: boolean; closing: boolean; reason: string };

/**
 * Close a session for good.
 *
 * `closing: true` means it was relayed to a runner and the row is STILL LISTED —
 * the runner deletes the emdash task and its next report retires the session, so
 * the client shows a pending state rather than removing the row itself. Removing
 * it optimistically would be a lie if the delete failed.
 *
 * Never throws on a refusal: `ok: false` with a `reason` to render.
 */
export function closeSession(id: string): Promise<CloseResult> {
  return request<CloseResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/close`, {
    method: "POST",
  });
}

export type Attachment = components["schemas"]["AttachmentOut"];

/**
 * Upload a file to a session. Multipart, so it deliberately does NOT go through
 * `request()` — that one sets a JSON content-type, and the browser must be left
 * to set its own multipart boundary.
 *
 * Returns before the message exists: the composer uploads while you are still
 * typing, and sending sweeps up whatever is attached to the session.
 */
export async function uploadAttachment(sessionId: string, file: File): Promise<Attachment> {
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(apiUrl(`/api/canopy-sessions/${sessionId}/attachments`), {
    method: "POST",
    credentials: "include",
    headers: { "X-CSRFToken": getCsrfToken() },
    body,
  });
  if (!res.ok) {
    // The server's 422s are the useful ones (wrong type, too large) — surface
    // the detail rather than a bare status, since the user can act on it.
    let detail = `upload failed (${res.status})`;
    try {
      const problem = await res.json();
      if (problem?.detail) detail = problem.detail;
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new Error(detail);
  }
  return res.json();
}

/** Discard an attachment that has not been sent yet (the chip's "x"). */
export async function deleteAttachment(attachmentId: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/canopy-sessions/attachments/${attachmentId}`), {
    method: "DELETE",
    credentials: "include",
    headers: { "X-CSRFToken": getCsrfToken() },
  });
  if (!res.ok && res.status !== 404) throw new Error(`delete failed (${res.status})`);
}

/** Where to render an attachment from. No signed URL to expire or leak — the
 *  server streams the bytes and gates on session membership. */
export function attachmentUrl(attachmentId: string): string {
  return apiUrl(`/api/canopy-sessions/attachments/${attachmentId}/content`);
}

/**
 * Answer the dialog a blocked agent is waiting on.
 *
 * `option = null` refuses (the runner sends Escape). A refusal comes back as a
 * 200 with `ok:false` and a reason rather than a 4xx: the dialog can go stale
 * between the phone rendering it and a thumb reaching it, and the runner can go
 * offline in between — both ordinary, neither a client error.
 */
export function answerMenu(
  id: string,
  option: number | null,
  selections?: number[][] | null,
): Promise<{ ok: boolean; reason: string }> {
  return request<{ ok: boolean; reason: string }>(
    `/api/canopy-sessions/${encodeURIComponent(id)}/answer-menu`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // `option` rides along with `selections` rather than being replaced by it:
      // it is the only field a runner older than this understands, and sending
      // it the first pick keeps such a runner doing what it does today instead
      // of reading a null option as "refuse" and pressing Escape.
      body: JSON.stringify(selections ? { option, selections } : { option }),
    },
  );
}
