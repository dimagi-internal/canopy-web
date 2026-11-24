/**
 * The messages between a dynamic view's frame and the canopy page that framed it.
 *
 * The frame (`frame/main.tsx`) is an opaque-origin document with no network; this
 * protocol is the whole of what it can do. The page (`DynamicViewFrame.tsx`) accepts
 * a message only from that frame's own `contentWindow`, and the frame accepts one only
 * from `window.parent` at an origin the server named. Design:
 * docs/architecture/dynamic-views.md.
 */

export const CHANNEL = 'canopy-dynamic-view'

/** What `canopy.connection.capabilities` lists, so a page can feature-check before relying on one:
 * `allow_duplicates` ("Create anyway"), `gesture_leases` (a drop or post-dialog write opens a lease).
 * The bundle client (apps/workspace_apps/bundle_client.js) lists the same. */
export const CONNECTION_CAPABILITIES = ['allow_duplicates', 'gesture_leases'] as const

/** What the view is drawn for — the `scope` prop. */
export interface ViewScope {
  workspace: { slug: string; name: string }
  project: { handle: string; name: string } | null
  view: { id: number; slug: string; title: string; version: number }
  viewer: { email: string; name: string }
}

export interface ReadRequest {
  model: string
  params?: Record<string, unknown>
  path?: Record<string, unknown>
}

/** A task's progress as a view sees it (`onStatus`, `canopy.agentTask`). */
export interface AgentTaskStatus {
  id: string
  agent: string
  name: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  error: string
  result?: unknown
  turn_id?: string | null
  deadline_at: string
  created_at: string
  finished_at?: string | null
}

/**
 * What a WORKSPACE APP's page is drawn for (apps/workspace_apps): which app, the
 * caller's roles in it, and the page's own parameters. Absent for a plain dynamic
 * view, whose frame then refuses every app operation.
 */
export interface AppContext {
  key: string
  name: string
  page: string
  params: Record<string, string>
  roles: string[]
  /** Drawn on the public page, for an anonymous visitor (or previewed as one). */
  public: boolean
  /** The app's collections and fields (labels, types, options) and its actions'
   * inputs — how a page draws a field the workspace added after it was written. */
  dataModel: Record<string, unknown>
  settings: Record<string, unknown>
  /** What the workspace put in this page's slots: {slot: [{key, code}]}. */
  fills: Record<string, { key: string; code: string }[]>
  /** Drawn on a draft's test copy. */
  draft: boolean
}

/** An app page's call out of the frame, answered by `read-result`. */
export type AppOp =
  | { op: 'query'; collection: string; where?: Record<string, unknown>; order?: string; limit?: number }
  | { op: 'act'; action: string; record?: number | null; input?: Record<string, unknown>; params?: Record<string, unknown> }
  /** One bulk action over many records; each runs alone ({done, failed}). */
  | { op: 'bulk'; action: string; records: number[]; input?: Record<string, unknown>; params?: Record<string, unknown> }
  /** A record's private link (to copy to a candidate): {url, days}. */
  | { op: 'link'; link: string; record: number }
  | { op: 'history'; record: number }
  | { op: 'upload'; collection: string; field: string; name: string; type: string; bytes: ArrayBuffer }
  | { op: 'open_file'; id: number }
  /** The tasks of the one project the app declares (`app.tasks.query`). Live work: refused in a draft. */
  | { op: 'tasks_query'; filters?: AppTaskFilters }
  /** One of the app's declared task actions, as the viewer (`app.tasks.act`). Never starts a turn. */
  | { op: 'tasks_act'; name: string; key?: string | null; input?: Record<string, unknown> }
  /** canopy's AI model (`canopy.ai.complete`), for a member page of an app that declares `ai`. */
  | { op: 'ai'; request: AiRequest }

/**
 * `canopy.ai.complete(request)` (docs/architecture/app-ai.md; checked by apps/workspace_apps/ai.py).
 * `model` is an alias, never a model id. A message's content is text or blocks: `text`,
 * `tool_use` (assistant; carry its `signature` back unread) and `tool_result` (user).
 */
export interface AiRequest {
  model?: 'fast' | 'smart'
  system?: string
  messages: { role: 'user' | 'assistant'; content: string | AiBlock[] }[]
  max_tokens?: number
  json_schema?: Record<string, unknown>
  tools?: { name: string; description?: string; input_schema?: Record<string, unknown> }[]
  web_search?: boolean
  /** The page's own label for the call, kept in canopy's log ("record-summary"). */
  feature?: string
}

export type AiBlock =
  | { type: 'text'; text: string; signature?: string }
  | { type: 'tool_use'; id: string; name: string; input: Record<string, unknown>; signature?: string }
  | { type: 'tool_result'; tool_use_id: string; content: unknown }

export interface AiAnswer {
  text: string
  content: AiBlock[]
  stop_reason: 'end_turn' | 'tool_use' | 'max_tokens' | 'refusal'
  model: 'fast' | 'smart'
  usage: { input_tokens: number; output_tokens: number }
  /** The parsed answer, when `json_schema` was asked (null if it did not parse). */
  json?: unknown
}

/** `app.tasks.query`'s filters (`GET /api/workspace-apps/{ws}/{key}/tasks`). */
export interface AppTaskFilters {
  status?: string
  category?: string
  due_after?: string
  due_before?: string
  template?: string
  recurring?: '' | 'true' | 'false'
  q?: string
  limit?: number
}

/**
 * What an app page drawn in the browser extension's panel may ask of the tab (its
 * manifest's `browser` entry), told to the frame so `canopy.browser` exists there. Absent
 * everywhere else, where `canopy.browser` is null. The canopy page enforces it; the frame
 * only knows it (src/ext/extensionBridge.ts).
 */
export interface BrowserInfo {
  adapter: string
  capabilities: string[]
  events: string[]
}

/** A `canopy.browser` call out of the frame, answered by `read-result`. */
export type BrowserOp =
  | { op: 'context' }
  | { op: 'request'; capability: string; args?: Record<string, unknown> }
  | { op: 'open'; url: string }

/**
 * A workspace app's BUNDLE page (apps/workspace_apps/bundles.py): the frame is one of the app's own
 * HTML files, running canopy's bundle client instead of compiling `code`. `pages` lets it open
 * another page of the app by its entry file (`canopy.open("kanban/kanban.html")`).
 */
export interface BundleInfo {
  pages: { slug: string; entry: string }[]
}

/** Page → frame. */
export type HostMessage =
  | { channel: typeof CHANNEL; type: 'render'; code: string; data: Record<string, unknown>; scope: ViewScope; theme: 'light' | 'dark'; app?: AppContext; browser?: BrowserInfo; bundle?: BundleInfo }
  // A declared adapter event on the tab in front of the person, or `context` when that tab changed.
  | { channel: typeof CHANNEL; type: 'browser-event'; event: string; data: unknown }
  | { channel: typeof CHANNEL; type: 'data'; data: Record<string, unknown> }
  | { channel: typeof CHANNEL; type: 'read-result'; id: number; ok: true; data: unknown }
  // `code` and `connect_url` come with a refused call to a connected account (`ConnectionRefusal`).
  | { channel: typeof CHANNEL; type: 'read-result'; id: number; ok: false; error: string; code?: string; connect_url?: string }
  | { channel: typeof CHANNEL; type: 'agent-task-status'; id: number; task: AgentTaskStatus }
  // canopy's panel calls an action the view offered (`canopy.page.registerAction`); the view
  // answers with a `page-action-result` of the same id.
  | { channel: typeof CHANNEL; type: 'page-action'; id: number; name: string; args: Record<string, unknown> }

/** Frame → page. */
export type FrameMessage =
  | { channel: typeof CHANNEL; type: 'ready' }
  | { channel: typeof CHANNEL; type: 'read'; id: number; request: ReadRequest }
  | { channel: typeof CHANNEL; type: 'refresh' }
  // `id` is the frame's request id; its answer is a `read-result` with the same id
  // (the task's result, or why it failed), after any number of `agent-task-status`.
  | { channel: typeof CHANNEL; type: 'agent-task'; id: number; request: unknown }
  | { channel: typeof CHANNEL; type: 'agent-task-read'; id: number; taskId: string }
  // Lend an agent the viewer's own Google account for a session (or end it): canopy asks
  // the viewer in its own dialog; the answer is a `read-result` with the same id.
  | { channel: typeof CHANNEL; type: 'google-access'; id: number; request: unknown }
  | { channel: typeof CHANNEL; type: 'connections'; id: number; request: unknown }
  // A call to a connected account (Salesforce), answered by a `read-result`. It carries no claim about
  // clicks: the page reads its own activation (connectionCalls.ts).
  | { channel: typeof CHANNEL; type: 'connection-call'; id: number; request: unknown }
  // Open the lease for this click, answered by a `read-result` with `{expires_at, hard_limit_at, writes}`.
  | { channel: typeof CHANNEL; type: 'connection-begin'; id: number }
  // Mark a task of the view's project done, or reopen it (`canopy.tasks.act`), answered by a `read-result`.
  // Like a connection write it carries no claim about clicks: the page reads its own activation (taskActions.ts).
  | { channel: typeof CHANNEL; type: 'task-action'; id: number; request: unknown }
  | { channel: typeof CHANNEL; type: 'navigate'; path: string }
  | { channel: typeof CHANNEL; type: 'app'; id: number; request: AppOp }
  // `newTab` (a bundle page's `canopy.open(…, {newTab: true})`) opens the page in a tab, from a click only.
  | { channel: typeof CHANNEL; type: 'go'; page: string; params: Record<string, string>; newTab?: boolean }
  // A pointerdown or keydown in the frame. No claim: the page believes it only if it sees a live click on
  // this frame as it arrives, and then lets a drop or a post-dialog write open a lease (gestures.ts).
  | { channel: typeof CHANNEL; type: 'gesture' }
  // `canopy.open(url)`: an https address in a new tab, from a click. Answered by a `read-result`.
  | { channel: typeof CHANNEL; type: 'open-url'; id: number; url: string }
  // `canopy.page.setParams`: replace this page's URL parameters, no reload. Answered by a `read-result`.
  | { channel: typeof CHANNEL; type: 'page-params'; id: number; params: Record<string, unknown> }
  // `canopy.clipboard.write(text)`, from a click. Answered by a `read-result`.
  | { channel: typeof CHANNEL; type: 'clipboard'; id: number; text: string }
  // `canopy.download(name, mime, data)`: save a file, from a click (or a gesture in the last minute).
  | { channel: typeof CHANNEL; type: 'download'; id: number; name: string; mime: string; data: unknown }
  // `canopy.browser` in the extension's panel: the tab's context, an adapter capability, opening a tab.
  | { channel: typeof CHANNEL; type: 'browser'; id: number; request: BrowserOp }
  // canopy's panel (pagePanel.ts): what the view shows, the actions it offers the agent,
  // its answers, and a question for the panel (only on a click on the view).
  | { channel: typeof CHANNEL; type: 'page-state'; state: unknown }
  | { channel: typeof CHANNEL; type: 'page-action-register'; spec: unknown }
  | { channel: typeof CHANNEL; type: 'page-action-unregister'; name: string }
  | { channel: typeof CHANNEL; type: 'page-action-result'; id: number; ok: boolean; data?: unknown; error?: string }
  | { channel: typeof CHANNEL; type: 'page-ask'; text: string }
  | { channel: typeof CHANNEL; type: 'resize'; height: number }
  | { channel: typeof CHANNEL; type: 'error'; message: string }

/** What a view's promise rejects with: `message` the sentence, `error` and `connect_url` when the page sent them. */
export function frameRejection(msg: { error: string; code?: string; connect_url?: string }): Error & { error?: string; connect_url?: string } {
  const e: Error & { error?: string; connect_url?: string } = new Error(msg.error)
  if (msg.code) e.error = msg.code
  if (msg.connect_url) e.connect_url = msg.connect_url
  return e
}

export function isMessage(d: unknown): d is { channel: string; type: string } {
  return !!d && typeof d === 'object' && (d as { channel?: unknown }).channel === CHANNEL
    && typeof (d as { type?: unknown }).type === 'string'
}
