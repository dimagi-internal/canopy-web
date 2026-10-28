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

/** Page → frame. */
export type HostMessage =
  | { channel: typeof CHANNEL; type: 'render'; code: string; data: Record<string, unknown>; scope: ViewScope; theme: 'light' | 'dark' }
  | { channel: typeof CHANNEL; type: 'data'; data: Record<string, unknown> }
  | { channel: typeof CHANNEL; type: 'read-result'; id: number; ok: true; data: unknown }
  | { channel: typeof CHANNEL; type: 'read-result'; id: number; ok: false; error: string }
  | { channel: typeof CHANNEL; type: 'agent-task-status'; id: number; task: AgentTaskStatus }

/** Frame → page. */
export type FrameMessage =
  | { channel: typeof CHANNEL; type: 'ready' }
  | { channel: typeof CHANNEL; type: 'read'; id: number; request: ReadRequest }
  | { channel: typeof CHANNEL; type: 'refresh' }
  // `id` is the frame's request id; its answer is a `read-result` with the same id
  // (the task's result, or why it failed), after any number of `agent-task-status`.
  | { channel: typeof CHANNEL; type: 'agent-task'; id: number; request: unknown }
  | { channel: typeof CHANNEL; type: 'agent-task-read'; id: number; taskId: string }
  | { channel: typeof CHANNEL; type: 'navigate'; path: string }
  | { channel: typeof CHANNEL; type: 'resize'; height: number }
  | { channel: typeof CHANNEL; type: 'error'; message: string }

export function isMessage(d: unknown): d is { channel: string; type: string } {
  return !!d && typeof d === 'object' && (d as { channel?: unknown }).channel === CHANNEL
    && typeof (d as { type?: unknown }).type === 'string'
}
