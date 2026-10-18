/**
 * The /ide session tree: filter the session list by what the viewer has toggled
 * on, then fold it into one folder per emdash project.
 *
 * Deliberately objective. Every filter and count reads a field the server
 * reports (running, a pending question, the mode, the runner) — nothing here
 * ranks a session as more urgent than another or calls it "stuck". Order is
 * stable: folders alphabetically, sessions by last activity, so rows do not
 * jump around because one of them was judged important.
 */

export type IdeSession = {
  id: string
  title: string
  status: string
  running?: boolean
  waiting_on_you?: boolean
  turn_mode?: string
  runner_id?: string | null
  runner_name?: string | null
  agent_slug?: string | null
  emdash_project?: string
  project?: string
  runner_reported?: boolean
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

/** The folder a session sits in: the emdash project its worktree lives under. */
export function folderKey(s: IdeSession): string {
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
  sessions: T[]
  counts: Record<IdeState, number>
}

const activityDesc = (a: IdeSession, b: IdeSession) =>
  Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at)

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
  const folders = [...byKey.entries()].map(([key, rows]) => {
    const counts: Record<IdeState, number> = { running: 0, input: 0, idle: 0 }
    for (const r of rows) counts[sessionState(r)] += 1
    return {
      key,
      isAgent: agentSlugs.has(key) || rows.some((r) => r.agent_slug === key),
      sessions: [...rows].sort(activityDesc),
      counts,
    }
  })
  return folders.sort((a, b) => {
    if (!a.key !== !b.key) return a.key ? -1 : 1
    return a.key.localeCompare(b.key)
  })
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
