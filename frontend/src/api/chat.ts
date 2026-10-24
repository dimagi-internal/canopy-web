/**
 * Client for the live chat surface (/api/canopy-sessions).
 *
 * Plain fetch rather than the generated openapi-fetch client (not yet ported:
 * the session-secrets and MCP Apps View routes are not in the schema) — the
 * live transcript's steady-state arrives over the WebSocket
 * (apps/canopy_sessions consumer), not REST. REST covers session
 * meta/create/list, scroll-back paging (`listMessages`), the viewer liveness
 * pair (`attachSession`/`detachSession`), and the runner backfill request
 * (`requestBackfill`). CSRF is attached for mutating calls; response shapes
 * reuse the generated OpenAPI schema so the two can't drift.
 */

import { offerForNewChat, type ConversationKeyOffer, type MyVaultKey as VaultKeyNamed } from "@/vault/sealedChat";
import type { Unlocked } from "@/vault/operations";
import type { components } from "./generated";
import { apiUrl, getCsrfToken } from "./base";
import { ApiError } from "./problem";
import type { AppHost, AppResourceUpdates, AppViewResource, CallToolResult } from "canopy-ui/chat";

export type ChatSession = components["schemas"]["SessionOut"];
export type ChatSessionDetail = components["schemas"]["SessionDetailOut"];
export type MessagePage = components["schemas"]["MessagePageOut"];
export type StreamState = components["schemas"]["StreamStateOut"];
export type BackfillState = components["schemas"]["BackfillStateOut"];
export type SendResult = components["schemas"]["SendOut"];
export type TurnPlacementResult = components["schemas"]["TurnOutMinimal"];
export type SlashCommand = components["schemas"]["SlashCommandOut"];

/** The shared `ApiError` (./problem) under its old name; callers branch on
 *  `.status` and `.code` (the problem type's tail, e.g. "not-found"). */
export { ApiError as ChatApiError };

/** A key pair for a chat this browser starts (`offerForNewChat`), or null when the person
 *  has no vault key (404: they cannot hold a private chat, and canopy refuses one).
 *  Anything else that stops it — a vault key this browser cannot confirm is theirs, a
 *  changed one, a failed read — THROWS, and the chat is not started: it never falls
 *  back to a key canopy-web would make. `derive` opens the vault key when this browser
 *  has not seen it before (`useVaultUnlock`). */
export async function browserConversationKey(
  mine: () => Promise<MyVaultKey | VaultKeyNamed>,
  derive?: () => Promise<Pick<Unlocked, "scalar" | "publicJwk">>,
): Promise<ConversationKeyOffer | null> {
  let me: VaultKeyNamed;
  try {
    me = (await mine()) as VaultKeyNamed;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
  // An answer that names no key says the same as a 404 (and is no weaker: the server
  // could as well say 404).
  if (!me || typeof me.jkt !== "string" || !me.public_jwk) return null;
  return offerForNewChat(me, derive);
}

// Still raw fetch (not apiV2): several routes here (the session's secrets, the
// MCP Apps View plumbing) are not in the generated schema. Errors go through the
// shared ApiError all the same.

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
  if (!resp.ok) throw ApiError.from(resp.status, body, `Request failed (${resp.status})`);
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
  // Opens the person's vault key, for a browser that has not confirmed it is theirs yet
  // (`browserConversationKey`). Without it such a browser cannot start a chat.
  unlock?: () => Promise<Pick<Unlocked, "scalar" | "publicJwk">>;
}

export async function createSession(
  input: CreateSessionInput = {},
): Promise<ChatSession> {
  const path = input.workspace
    ? `/api/w/${input.workspace}/canopy-sessions/`
    : "/api/canopy-sessions/";
  // The conversation's key is made HERE: if it is, or becomes, private its words are
  // sealed to it, and canopy-web never sees the private half (vault/sealedChat.ts).
  const conversationKey = await browserConversationKey(myVaultKey, input.unlock);
  return request<ChatSession>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: input.title ?? "",
      agent_slug: input.agentSlug ?? null,
      project: input.project ?? "",
      metadata: input.metadata ?? {},
      runner_id: input.runnerId ?? null,
      ...(conversationKey ? { conversation_key: conversationKey } : {}),
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

/**
 * The list URL for a state. `active` is the server default, so it sends no param.
 * `reply` asks for each session's last agent reply (`last_reply` /
 * `agent_spoke_last`) — the supervisor feed's fields, opt-in so the other list
 * callers do not carry text they never render.
 */
export function sessionsPath(
  state: SessionState = "active",
  opts: { reply?: boolean; limit?: number } = {},
): string {
  const params = new URLSearchParams();
  if (state !== "active") params.set("state", state);
  if (opts.reply) params.set("reply", "true");
  if (opts.limit) params.set("limit", String(opts.limit));
  const q = params.toString();
  // A user-level view: every page that lists sessions (Supervisor, the IDE,
  // the chat home) spans your workspaces, and this one read covers them all —
  // the server reads each organisation on its own.
  return q ? `/api/me/sessions/?${q}` : "/api/me/sessions/";
}

export function listSessions(
  state: SessionState = "active",
  opts: { reply?: boolean; limit?: number } = {},
): Promise<ChatSession[]> {
  return request<ChatSession[]>(sessionsPath(state, opts));
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
  /** "Clear & send": the runner deletes any unsent text already in the
   *  session's prompt and delivers this, instead of asking on its own screen. */
  clearPrompt = false,
): Promise<SendResult> {
  return request<SendResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      client_id: clientId,
      placement: placement ?? null,
      ...(clearPrompt ? { clear_prompt: true } : {}),
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

/**
 * Push on every finished turn in this session (`true`), or only once it has
 * gone quiet for the user's chosen minutes (`false`, the default).
 */
export function setSessionNotify(id: string, everyCompletion: boolean): Promise<ChatSession> {
  return request<ChatSession>(`/api/canopy-sessions/${encodeURIComponent(id)}/notify`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ every_completion: everyCompletion }),
  });
}

/**
 * Rename this session. A typed title is never replaced by the emdash task name
 * or the first message; blank clears it. The emdash task keeps its own name.
 */
export function setSessionTitle(id: string, title: string): Promise<ChatSession> {
  return request<ChatSession>(`/api/canopy-sessions/${encodeURIComponent(id)}/title`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export type SessionInfo = components["schemas"]["SessionInfoOut"];
export type SessionInfoTurn = components["schemas"]["TurnOut"];

/**
 * Everything canopy stores about one session, for the Session info panel: the
 * session (who opened it, what created it, under which parent, what it did), the
 * runner binding behind it, and the turns that drove it.
 */
export function getSessionInfo(id: string): Promise<SessionInfo> {
  return request<SessionInfo>(`/api/canopy-sessions/${encodeURIComponent(id)}/info`);
}

export type SlackDetachResult = components["schemas"]["SlackDetachOut"];

/**
 * Disconnect this session from its Slack thread: its replies stop posting there
 * and replies in the thread stop reaching it. The thread is told once; nothing
 * already posted is deleted. Idempotent — `status: "not_bound"` when there was
 * no thread. Share it with "Keep the thread connected" again to re-attach (a
 * fresh thread).
 */
export function detachSessionFromSlack(id: string): Promise<SlackDetachResult> {
  return request<SlackDetachResult>(`/api/canopy-sessions/slack/detach`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: id }),
  });
}

/**
 * File this session in another workspace you can edit in. Only the person whose
 * session it is (its creator, or the owner of the runner it was found on) may;
 * an agent's session refuses with a 422. Members of the old workspace who are
 * not in the new one lose it.
 */
export function moveSession(id: string, workspace: string): Promise<ChatSession> {
  return request<ChatSession>(`/api/canopy-sessions/${encodeURIComponent(id)}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace }),
  });
}

export type TransferResult = components["schemas"]["TransferOut"];

/**
 * Move a live session onto another runner (by id or name), history and all.
 *
 * `status: "moved"` means it happened now — `runner` names where it landed.
 * `status: "pending"` means the target belongs to someone else, so this
 * opened a TransferRequest instead and `approvers` lists who can say yes;
 * nothing has moved yet.
 *
 * Throws `ChatApiError` on refusal: 409 while a turn is executing on the
 * source (stop the session and retry) or while another request for it is
 * already waiting, 422 for a target the session can't move onto (already
 * there, or archived), 404 for an unknown runner or an unbound session.
 */
export function transferSession(id: string, runner: string, brief = ""): Promise<TransferResult> {
  return request<TransferResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/transfer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runner, brief }),
  });
}

/**
 * Start a new session from this one's conversation up to the end of the turn
 * `turnIndex` belongs to, on the same runner and in the same worktree.
 *
 * Throws `ChatApiError`: 422 when the session cannot be forked (its runner is not
 * on the Claude CLI runtime, or the message predates a transfer), 409 when the cut
 * would fall inside a turn that is still running, 404 for an unknown message.
 */
export function forkSession(id: string, turnIndex: number): Promise<ChatSession> {
  return request<ChatSession>(`/api/canopy-sessions/${encodeURIComponent(id)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ turn_index: turnIndex }),
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

/** Retire a session row by hand without touching emdash; /unarchive undoes it. */
export function archiveSession(id: string): Promise<ChatSession> {
  return request<ChatSession>(`/api/canopy-sessions/${encodeURIComponent(id)}/archive`, { method: "POST" });
}

export function unarchiveSession(id: string): Promise<ChatSession> {
  return request<ChatSession>(`/api/canopy-sessions/${encodeURIComponent(id)}/unarchive`, { method: "POST" });
}

export type StopResult = { cancelled: boolean; interrupted: boolean; route: string };

/** The same stop as the chat's stop button: cancel open turns, else interrupt the agent. */
export function stopSession(id: string): Promise<StopResult> {
  return request<StopResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/stop`, { method: "POST" });
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
    const problem = await res.json().catch(() => undefined);
    throw ApiError.from(res.status, problem, `upload failed (${res.status})`);
  }
  return res.json();
}

/** The commands the composer offers after `/` (`useSlashCommands`). */
export function listSlashCommands(sessionId: string): Promise<SlashCommand[]> {
  return request<SlashCommand[]>(`/api/canopy-sessions/${sessionId}/slash-commands`);
}

/** Discard an attachment that has not been sent yet (the chip's "x"). */
export async function deleteAttachment(attachmentId: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/canopy-sessions/attachments/${attachmentId}`), {
    method: "DELETE",
    credentials: "include",
    headers: { "X-CSRFToken": getCsrfToken() },
  });
  if (!res.ok && res.status !== 404) {
    throw ApiError.from(res.status, await res.json().catch(() => undefined), `delete failed (${res.status})`);
  }
}

/** A file sent in a private conversation, as its people's browsers open it (no name). */
export type SealedAttachment = components["schemas"]["SealedAttachmentOut"];

/** Store the people's sealed copy of a file in a private conversation
 *  (`vault/sealedFiles.ts::sealAttachment`). Once, by its uploader, before it is sent. */
export async function uploadSealedCopy(attachmentId: string, sealedName: string, blob: Uint8Array): Promise<void> {
  const body = new FormData();
  body.append("sealed_name", sealedName);
  body.append("file", new Blob([blob as Uint8Array<ArrayBuffer>], { type: "application/octet-stream" }), "sealed");
  const res = await fetch(apiUrl(`/api/canopy-sessions/attachments/${attachmentId}/sealed`), {
    method: "POST",
    credentials: "include",
    headers: { "X-CSRFToken": getCsrfToken() },
    body,
  });
  if (!res.ok) {
    throw ApiError.from(res.status, await res.json().catch(() => undefined), `sealing failed (${res.status})`);
  }
}

/** The sealed files sent in a private conversation, each at the line it was sent with. */
export function listSealedAttachments(sessionId: string): Promise<SealedAttachment[]> {
  return request<SealedAttachment[]>(`/api/canopy-sessions/${encodeURIComponent(sessionId)}/sealed-attachments`);
}

/** A sealed file's ciphertext, for this browser to open with the conversation key. */
export async function fetchSealedAttachment(attachmentId: string): Promise<Uint8Array> {
  const res = await fetch(apiUrl(`/api/canopy-sessions/attachments/${attachmentId}/sealed`), {
    credentials: "include",
  });
  if (!res.ok) {
    throw ApiError.from(res.status, await res.json().catch(() => undefined), `could not fetch the file (${res.status})`);
  }
  return new Uint8Array(await res.arrayBuffer());
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
  texts?: (string | null)[] | null,
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
      body: JSON.stringify(selections ? { option, selections, texts } : { option }),
    },
  );
}

export type Participant = components["schemas"]["ParticipantOut"];

/** Everyone explicitly given this chat. */
export function listParticipants(id: string): Promise<Participant[]> {
  return request<Participant[]>(`/api/canopy-sessions/${encodeURIComponent(id)}/participants`);
}

export type SealedKeyWrapIn = components["schemas"]["SealedKeyWrapIn"];
export type ParticipantRecipient = components["schemas"]["ParticipantRecipientOut"];

/** Owner only: give a workspace teammate this chat, or change their role. In a
 *  sensitive conversation a NEW person needs `wrap`: its key, wrapped in this
 *  browser to their vault key (vault/sealedChat.ts::wrapForRecipient). */
export function addParticipant(id: string, email: string, role: "editor" | "viewer",
                               wrap?: SealedKeyWrapIn): Promise<Participant[]> {
  return request<Participant[]>(`/api/canopy-sessions/${encodeURIComponent(id)}/participants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(wrap ? { email, role, wrap } : { email, role }),
  });
}

/** The vault key to wrap a sensitive chat's key to, before adding `email`. */
export function participantRecipient(id: string, email: string): Promise<ParticipantRecipient> {
  return request<ParticipantRecipient>(
    `/api/canopy-sessions/${encodeURIComponent(id)}/participants/recipient?email=${encodeURIComponent(email)}`);
}

/** Give someone already in a private chat their copy of its key — they were given the
 *  chat before it became private, or replaced their vault key. `wrap` is made in this
 *  browser (vault/sealedChat.ts::wrapForRecipient). */
export function giveKeyCopy(id: string, userId: number, wrap: SealedKeyWrapIn): Promise<Participant[]> {
  return request<Participant[]>(`/api/canopy-sessions/${encodeURIComponent(id)}/key-copies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, wrap }),
  });
}

export type MyKeyCopy = components["schemas"]["MyKeyCopyOut"];
export type MyVaultKey = components["schemas"]["MyVaultKeyOut"];

/** Every private chat whose key I hold a copy of (to re-wrap before replacing my vault key). */
export function myKeyCopies(): Promise<MyKeyCopy[]> {
  return request<MyKeyCopy[]>("/api/canopy-sessions/my-keys");
}

/** Store my copies, re-wrapped to my NEW vault key. */
export function putMyKeyCopies(copies: { session_id: string; wrap: { alg: "ecdh-es"; epk: string; n: string; ct: string };
                                         recipient_jkt: string }[]): Promise<{ stored: number }> {
  return request<{ stored: number }>("/api/canopy-sessions/my-keys", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ copies }),
  });
}

/** My vault key's public half, for this browser to wrap a new chat's key to. */
export function myVaultKey(): Promise<MyVaultKey> {
  return request<MyVaultKey>("/api/canopy-sessions/my-vault-key");
}

/** Your copy of a private chat's key does not open: drop it, so someone in it can give
 *  you a fresh one (`POST /{id}/key-copies`). Only you can, for your own copy. */
export function setAsideMyKeyCopy(id: string): Promise<Participant[]> {
  return request<Participant[]>(
    `/api/canopy-sessions/${encodeURIComponent(id)}/key-copies/mine`,
    { method: "DELETE" },
  );
}

/** Owner removes someone; anyone can remove themselves. */
export function removeParticipant(id: string, userId: number): Promise<Participant[]> {
  return request<Participant[]>(
    `/api/canopy-sessions/${encodeURIComponent(id)}/participants/${userId}`,
    { method: "DELETE" },
  );
}

export type JoinRequest = components["schemas"]["JoinRequestOut"];

/** A private conversation you can see but are not in: ask its owner to add you.
 *  Carries nothing the conversation said; asking again returns the open request. */
export function askToJoin(id: string): Promise<JoinRequest> {
  return request<JoinRequest>(`/api/canopy-sessions/${encodeURIComponent(id)}/join-requests`,
    { method: "POST" });
}

/** Its owner: the people asking to be added (empty for anyone else). */
export function listJoinRequests(id: string): Promise<JoinRequest[]> {
  return request<JoinRequest[]>(`/api/canopy-sessions/${encodeURIComponent(id)}/join-requests`);
}

/** Its owner keeps them out. (Letting them in is `addParticipant`, which answers it.) */
export function declineJoinRequest(requestId: string): Promise<JoinRequest> {
  return request<JoinRequest>(
    `/api/canopy-sessions/join-requests/${encodeURIComponent(requestId)}/decline`, { method: "POST" });
}

export interface SharedSecret {
  name: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  /** Deleted 30 minutes after it was shared. */
  expires_at: string;
}

/**
 * Hand this chat a secret. Nothing is posted into the chat: mention it by name,
 * and the session bound to this chat finds it with `canopy secret list`.
 */
export function shareSecret(id: string, name: string, value: string): Promise<SharedSecret> {
  return request(`/api/canopy-sessions/${encodeURIComponent(id)}/secrets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, value }),
  });
}

export function listSecrets(id: string): Promise<SharedSecret[]> {
  return request(`/api/canopy-sessions/${encodeURIComponent(id)}/secrets`);
}

export function deleteSecret(id: string, name: string): Promise<void> {
  return request(
    `/api/canopy-sessions/${encodeURIComponent(id)}/secrets/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

// --- MCP Apps (spec 2026-10-08): the browser plumbing behind a rendered View ---

/** How canopy's own chat page reaches a session's MCP Apps Views. Every call
 *  runs as the SIGNED-IN person (session cookie) — the person looking, never
 *  the agent. A sandbox URL (absolute, if the sandbox ever moves to its own
 *  origin) passes through; a path gets the app's base. */
export function appHostFor(sessionId: string, updates?: AppResourceUpdates): AppHost {
  const base = `/api/canopy-sessions/${encodeURIComponent(sessionId)}/apps`;
  const at = (callId: string) => `${base}/${encodeURIComponent(callId)}`;
  const json = (method: string, body: unknown): RequestInit => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return {
    load: (callId) => request<AppViewResource>(`${at(callId)}/resource`),
    callTool: (callId, name, args) =>
      request<CallToolResult>(`${at(callId)}/call`, json("POST", { name, arguments: args })),
    readResource: (callId, uri) => request(`${at(callId)}/read`, json("POST", { uri })),
    updateContext: (callId, params) =>
      request<void>(`${at(callId)}/context`, json("PUT", params)).then(() => undefined),
    sendMessage: (callId, text) =>
      request<void>(`${at(callId)}/message`, json("POST", { text, client_id: `appview-${crypto.randomUUID()}` }))
        .then(() => undefined),
    // A View's `resources/subscribe`: canopy holds it; the host's update comes
    // back over the session socket (`app.resource_updated`, into `updates`).
    subscribe: (callId, uri) =>
      request<void>(`${at(callId)}/subscribe`, json("POST", { uri })).then(() => undefined),
    unsubscribe: (callId, uri) =>
      request<void>(`${at(callId)}/unsubscribe`, json("POST", { uri })).then(() => undefined),
    ...(updates ? { onResourceUpdated: updates.onResourceUpdated } : {}),
    resolveUrl: (path) => (/^https?:\/\//.test(path) ? path : apiUrl(path)),
    // A View may ask to fill the window (a results table, a chart).
    fullscreen: true,
  };
}
