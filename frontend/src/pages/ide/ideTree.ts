/**
 * The /ide session tree: filter the session list by what the viewer has toggled
 * on, then fold it into one folder per emdash project.
 *
 * Deliberately objective. Every filter and count reads a field the server
 * reports (running, a pending question, the mode, the runner) — nothing here
 * ranks a session as more urgent than another or calls it "stuck". Order is
 * stable: folders alphabetically, sessions by last activity, so rows do not
 * jump around because one of them was judged important.
 *
 * Every session sits in its own place. A session another one was started from
 * (forked, Slack-branched, dispatched) also carries a LINK to it underneath —
 * a reference to the real row, never a move (`childLinks`).
 */

import { scopedPath } from '@/lib/scopedLinks'

export type IdeSession = {
  id: string
  title: string
  status: string
  running?: boolean
  waiting_on_you?: boolean
  turn_mode?: string
  runner_id?: string | null
  runner_name?: string | null
  /** The bound runner is online but Claude is not signed in on it. */
  runner_signed_out?: boolean
  agent_slug?: string | null
  emdash_project?: string
  project?: string
  runner_reported?: boolean
  /** The session this one was started from: forked, Slack-branched or dispatched. */
  parent_session_id?: string | null
  /** What created it; a fork records `forked_from`. */
  provenance?: { [key: string]: unknown }
  last_activity_at: string
}

/** What a session is doing right now, as reported — one of three, never inferred. */
export type IdeState = 'running' | 'input' | 'idle'

/** The mode its driving turn ran in; `none` when no turn has been claimed yet
 *  (a session started by hand in emdash has no canopy turn behind it). */
export type IdeMode = 'manual' | 'auto' | 'none'

/** The key unbound (web) sessions file under in the runner filter. */
export const NO_RUNNER = 'none'

export type IdeFilters = {
  /** Runner ids switched OFF (an id absent here is shown, so a new runner appears). */
  hiddenRunners: string[]
  states: Record<IdeState, boolean>
  modes: Record<IdeMode, boolean>
  archived: boolean
  query: string
}

export const DEFAULT_FILTERS: IdeFilters = {
  hiddenRunners: [],
  states: { running: true, input: true, idle: true },
  modes: { manual: true, auto: true, none: true },
  archived: false,
  query: '',
}

export function sessionState(s: IdeSession): IdeState {
  // A pending question outranks `running`: the engine is parked on it, which
  // is exactly what the runner reports when it says the session awaits input.
  if (s.waiting_on_you) return 'input'
  if (s.running) return 'running'
  return 'idle'
}

export function sessionMode(s: IdeSession): IdeMode {
  return s.turn_mode === 'manual' || s.turn_mode === 'auto' ? s.turn_mode : 'none'
}

export function runnerKey(s: IdeSession): string {
  return s.runner_id || NO_RUNNER
}

/** The folder a session sits in: the emdash project its worktree lives under.
 *  Home groups its sessions by the same key. */
export function folderKey(s: Pick<IdeSession, 'emdash_project' | 'project' | 'agent_slug'>): string {
  return (s.emdash_project || s.project || s.agent_slug || '').trim()
}

export function matches(s: IdeSession, f: IdeFilters): boolean {
  if (!f.archived && s.status === 'archived') return false
  if (f.hiddenRunners.includes(runnerKey(s))) return false
  if (!f.states[sessionState(s)]) return false
  if (!f.modes[sessionMode(s)]) return false
  const q = f.query.trim().toLowerCase()
  if (q && !`${s.title} ${folderKey(s)} ${s.runner_name ?? ''}`.toLowerCase().includes(q)) return false
  return true
}

export type IdeFolder<T extends IdeSession = IdeSession> = {
  key: string
  /** The folder is an agent's own repo (its sessions are chats with that agent). */
  isAgent: boolean
  /** Newest activity first. */
  sessions: T[]
  counts: Record<IdeState, number>
}

const activity = (s: IdeSession) => Date.parse(s.last_activity_at) || 0
const byNewest = (a: IdeSession, b: IdeSession) => activity(b) - activity(a)

/** Filtered sessions, one folder per emdash project, folders A→Z with the
 *  unfiled ("") folder last, sessions newest-activity first. */
export function buildTree<T extends IdeSession>(
  sessions: readonly T[],
  filters: IdeFilters,
  agentSlugs: ReadonlySet<string> = new Set(),
): IdeFolder<T>[] {
  const byKey = new Map<string, T[]>()
  for (const s of sessions) {
    if (!matches(s, filters)) continue
    const key = folderKey(s)
    const list = byKey.get(key)
    if (list) list.push(s)
    else byKey.set(key, [s])
  }
  const folders = [...byKey.entries()].map(([key, list]) => {
    const rows = [...list].sort(byNewest)
    const counts: Record<IdeState, number> = { running: 0, input: 0, idle: 0 }
    for (const r of rows) counts[sessionState(r)] += 1
    return {
      key,
      isAgent: agentSlugs.has(key) || rows.some((r) => r.agent_slug === key),
      sessions: rows,
      counts,
    }
  })
  return folders.sort((a, b) => {
    if (!a.key !== !b.key) return a.key ? -1 : 1
    return a.key.localeCompare(b.key)
  })
}

/** How a linked session came from its parent, for the link's words. */
export function linkKind(s: IdeSession): 'fork' | 'started' {
  return s.provenance?.forked_from ? 'fork' : 'started'
}

/** parent id → the sessions started from it, newest first: the links shown
 *  under the parent's row. Drawn from the list the server gave the viewer — it
 *  holds only sessions they can read, so a child they cannot open is never
 *  linked. A link is a reference, so the runner/state/mode filters do not hide
 *  it (its own row may be hidden; the link still opens it); an archived child
 *  is linked only while archived sessions are shown. */
export function childLinks<T extends IdeSession>(sessions: readonly T[], filters: IdeFilters): Map<string, T[]> {
  const out = new Map<string, T[]>()
  for (const s of sessions) {
    const parent = s.parent_session_id
    if (!parent || parent === s.id) continue
    if (!filters.archived && s.status === 'archived') continue
    const list = out.get(parent)
    if (list) list.push(s)
    else out.set(parent, [s])
  }
  for (const list of out.values()) list.sort(byNewest)
  return out
}

/** What deleting a session does, in the words a confirm shows. Only a session
 *  a runner reports as an emdash task has a task to delete; a cloud session or
 *  an unbound web chat has its turns cancelled and is archived. */
export function deleteConsequence(s: IdeSession): string {
  if (s.runner_reported && s.runner_name) return `Deletes its emdash task on ${s.runner_name}.`
  if (s.runner_name) return `Cancels its turns on ${s.runner_name} and archives it.`
  return 'Archives it (it has nothing on a runner).'
}

/** Compact age for a dense row: "now", "12m", "3h", "2d". */
export function shortAge(iso: string, now: Date): string {
  const secs = Math.max(0, Math.floor((now.getTime() - Date.parse(iso)) / 1000))
  if (secs < 60) return 'now'
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

/** What dragging a row into a composer types: a markdown link to the session's
 *  page, its title as the text. The id in the URL is what an agent reads the
 *  session by; the title is for the people in the conversation. */
export function sessionReference(
  s: { id: string; workspace: string },
  label: string,
  origin: string,
): string {
  const text = label.replace(/[[\]\\]/g, '\\$&')
  return `[${text}](${origin}${scopedPath(s.workspace, `/chat/${s.id}`)})`
}
