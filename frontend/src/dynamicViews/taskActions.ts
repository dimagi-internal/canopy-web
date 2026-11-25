/**
 * A project view marking one of its project's tasks done, or reopening it, as the viewer.
 *
 * The view's code calls `canopy.tasks.act(key, action)`; the frame posts it here and the page
 * POSTs `/api/dynamic-views/{id}/task-actions` with the VIEWER's own session
 * (apps/dynamic_views/task_actions.py), where the view's current declaration, the task's project and
 * the viewer's edit rights on the task decide. Before anything is sent this refuses:
 *
 * - anything but a click on this screen — `clicked` is the page's OWN reading of its activation
 *   (`pageClickedFrame`), taken when the message arrived, before any await; the frame's message
 *   carries no click claim at all;
 * - an action the view does not declare (`task_actions`), or one that is not a status move;
 * - an unsaved preview (no view to act as) and a workspace app's page (which has `app.tasks.act`).
 *
 * Free of React and of the network (the client is passed in): taskActions.test.ts.
 */

export const TASK_ACTIONS = ['done', 'reopen'] as const
export type TaskAction = (typeof TASK_ACTIONS)[number]

export const TASK_NEEDS_CLICK = 'Changing a task needs a click: call canopy.tasks.act straight from a button.'

/** What the screen is, read fresh on each call. */
export interface TaskActionContext {
  /** The saved view's id; null for an unsaved preview. */
  viewId: number | null
  /** The view's `task_actions`. */
  declared: readonly string[]
  /** A workspace app's page, not a view. */
  app: boolean
}

export type TaskActionClient = (viewId: number, key: string, action: TaskAction) => Promise<unknown>

export type CheckedTaskAction = { ok: true; viewId: number; key: string; action: TaskAction } | { ok: false; error: string }

export function checkTaskAction(ctx: TaskActionContext, req: unknown, clicked: boolean): CheckedTaskAction {
  if (ctx.app) return { ok: false, error: 'An app page changes its tasks with app.tasks.act, not canopy.tasks.act.' }
  if (!req || typeof req !== 'object' || Array.isArray(req)) return { ok: false, error: 'canopy.tasks.act takes (key, action)' }
  const { key, action } = req as Record<string, unknown>
  if (typeof key !== 'string' || !key.trim() || typeof action !== 'string') {
    return { ok: false, error: 'canopy.tasks.act takes a task key and an action, e.g. ("CON-12", "done")' }
  }
  if (!(TASK_ACTIONS as readonly string[]).includes(action) || !(ctx.declared ?? []).includes(action)) {
    return { ok: false, error: `This view does not declare the task action "${action}" (its task_actions).` }
  }
  if (ctx.viewId === null) return { ok: false, error: 'Save this view to change tasks from it.' }
  if (!clicked) return { ok: false, error: TASK_NEEDS_CLICK }
  return { ok: true, viewId: ctx.viewId, key: key.trim(), action: action as TaskAction }
}

/** Check, then send. Rejects with the refusal's sentence, here or from the server. */
export async function runTaskAction(client: TaskActionClient, ctx: TaskActionContext, req: unknown, clicked: boolean): Promise<unknown> {
  const c = checkTaskAction(ctx, req, clicked)
  if (!c.ok) throw new Error(c.error)
  return client(c.viewId, c.key, c.action)
}
