// What a task list is showing, read from and written to its URL — so a
// filtered, grouped, sorted list (and its layout) is a link you can share, and
// a saved view is nothing more than a stored set of these parameters. One
// parser and one writer, so the filter bar, the tabs, a saved view and an old
// agent-board link all mean the same thing by the same word.
import type { SavedView, TaskQuery } from '@/api/work'

import type { Category } from './meta'
import type { GroupBy } from './TaskTable'

export type TabId = 'mine' | 'waiting' | 'created' | 'all'
export type Layout = 'table' | 'board' | 'calendar'
export type DueFilter = '' | 'overdue' | 'week' | 'none'

export const TABS: { id: TabId; label: string; query: TaskQuery }[] = [
  { id: 'mine', label: 'My tasks', query: { assignee: 'me' } },
  { id: 'waiting', label: 'Waiting on me', query: { waiting: 'me' } },
  { id: 'created', label: 'Created by me', query: { created_by: 'me' } },
  { id: 'all', label: 'All', query: {} },
]

export const ALL_TABS: TabId[] = TABS.map((t) => t.id)

export const CATEGORIES: Category[] = ['triage', 'backlog', 'unstarted', 'started', 'completed', 'canceled']

/** Open work — what a list shows until you ask for more. */
export const OPEN_CATEGORIES: Category[] = ['triage', 'backlog', 'unstarted', 'started']

export const SORTS: { value: string; label: string }[] = [
  { value: '', label: 'Asks first' },
  { value: 'rank', label: 'Manual' },
  { value: 'priority', label: 'Priority' },
  { value: 'due', label: 'Due date' },
  { value: 'updated', label: 'Updated' },
  { value: 'created', label: 'Created' },
  { value: 'status', label: 'Status' },
  { value: 'title', label: 'Title' },
]

export const GROUPS: { value: GroupBy; label: string }[] = [
  { value: '', label: 'No grouping' },
  { value: 'status', label: 'Status' },
  { value: 'project', label: 'Project' },
  { value: 'assignee', label: 'Assignee' },
  { value: 'priority', label: 'Priority' },
]

export interface ViewState {
  tab: TabId
  /** Named in the URL (or by an old `?waiting=me` link), not defaulted. */
  tabExplicit: boolean
  /** [] = every category. */
  categories: Category[]
  assignee: string
  project: string
  label: string
  priority: string
  kind: string
  due: DueFilter
  q: string
  group: GroupBy
  sort: string
  layout: Layout
  /** One sitting of an agent's (an old `?batch=` link): settled tasks included. */
  batch: string
  /** The calendar's month, `YYYY-MM` ("" = this month). */
  month: string
  /** The saved view these parameters came from, by id. */
  saved: string
}

const DUES: DueFilter[] = ['overdue', 'week', 'none']
const LAYOUTS: Layout[] = ['table', 'board', 'calendar']
const GROUP_VALUES = GROUPS.map((g) => g.value)

function pickCategories(raw: string): Category[] {
  return raw.split(',').filter((c): c is Category => (CATEGORIES as string[]).includes(c))
}

/** Read a list's state from its URL. `tabs` are the tabs this list offers
 *  (the first is the default); a list without tabs shows everything. */
export function parseView(params: URLSearchParams, tabs: readonly TabId[]): ViewState {
  const rawTab = params.get('tab') as TabId | null
  let tab: TabId = tabs[0] ?? 'all'
  let tabExplicit = false
  if (rawTab && tabs.includes(rawTab)) {
    tab = rawTab
    tabExplicit = true
  } else if (params.get('waiting') === 'me' && tabs.includes('waiting')) {
    // The address every "waiting on you" link (and the old Inbox) uses.
    tab = 'waiting'
    tabExplicit = true
  }
  const batch = params.get('batch') ?? ''
  const rawCat = params.get('category')
  const categories =
    rawCat === 'all'
      ? []
      : rawCat
        ? pickCategories(rawCat)
        : params.get('view') === 'done'
          ? (['completed', 'canceled'] as Category[])
          : batch
            ? []
            : OPEN_CATEGORIES
  const due = params.get('due') as DueFilter | null
  const layout = params.get('layout') as Layout | null
  const group = (params.get('group') ?? (params.get('by') === 'project' ? 'project' : '')) as GroupBy
  return {
    tab,
    tabExplicit,
    categories,
    assignee: params.get('assignee') ?? '',
    project: params.get('project') ?? '',
    label: params.get('label') ?? '',
    priority: params.get('priority') ?? '',
    kind: params.get('kind') ?? '',
    due: due && DUES.includes(due) ? due : '',
    q: params.get('q') ?? '',
    group: GROUP_VALUES.includes(group) ? group : '',
    sort: params.get('sort') ?? '',
    layout: layout && LAYOUTS.includes(layout) ? layout : 'table',
    batch,
    month: params.get('month') ?? '',
    saved: params.get('saved') ?? '',
  }
}

/** Fold an old agent-board link's words (`waiting=me`, `view=done`,
 *  `by=project`) into this list's own, before any write — so a filter change on
 *  a page opened from an old link keeps what the link asked for. */
export function normalizeParams(prev: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(prev)
  if (next.get('waiting') === 'me' && !next.get('tab')) next.set('tab', 'waiting')
  next.delete('waiting')
  if (next.get('view') === 'done' && !next.get('category')) next.set('category', 'completed,canceled')
  next.delete('view')
  if (next.get('by') === 'project' && !next.get('group')) next.set('group', 'project')
  next.delete('by')
  return next
}

/** Write the categories the way `parseView` reads them back. */
export function setCategories(next: URLSearchParams, cats: readonly Category[]): void {
  const sorted = CATEGORIES.filter((c) => cats.includes(c))
  if (sorted.length === 0) next.set('category', 'all')
  else if (sorted.join(',') === OPEN_CATEGORIES.join(',')) next.delete('category')
  else next.set('category', sorted.join(','))
}

export function isoDay(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

function addDays(d: Date, n: number): Date {
  const x = new Date(d)
  x.setDate(x.getDate() + n)
  return x
}

/** The server query for a list's state; `fixed` (what the caller pins) wins. */
export function toQuery(state: ViewState, fixed: TaskQuery = {}, today: Date = new Date()): TaskQuery {
  const tab = TABS.find((t) => t.id === state.tab)?.query ?? {}
  const q: Record<string, string | number | null | undefined> = { ...tab }
  if (state.categories.length) q.category = state.categories.join(',')
  if (state.assignee) q.assignee = state.assignee
  if (state.project) q.project = state.project
  if (state.label) q.label = state.label
  if (state.priority) q.priority = state.priority
  if (state.kind) q.kind = state.kind
  if (state.q) q.q = state.q
  if (state.batch) q.batch = state.batch
  if (state.due === 'overdue') q.due_before = isoDay(addDays(today, -1))
  if (state.due === 'week') {
    q.due_after = isoDay(today)
    q.due_before = isoDay(addDays(today, 6))
  }
  // A board is ordered by hand: its columns read in rank order unless asked otherwise.
  const sort = state.sort || (state.layout === 'board' ? 'rank' : '')
  if (sort) q.sort = sort
  return { ...q, ...fixed } as TaskQuery
}

/** A saved view's `filters`, from the list's state (the server's VIEW_FILTERS keys). */
export function viewFilters(state: ViewState): Record<string, string> {
  const f: Record<string, string> = {}
  if (state.tabExplicit) f.tab = state.tab
  const cats = state.categories.join(',')
  if (cats !== OPEN_CATEGORIES.join(',')) f.category = cats || 'all'
  for (const k of ['assignee', 'project', 'label', 'priority', 'kind', 'due', 'q'] as const) {
    if (state[k]) f[k] = state[k]
  }
  return f
}

const VIEW_KEYS = ['tab', 'category', 'assignee', 'project', 'label', 'priority', 'kind', 'due', 'q'] as const

/** The URL a saved view opens: exactly its parameters (and its id). */
export function viewParams(view: SavedView, keep?: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams()
  const peek = keep?.get('peek')
  if (peek) next.set('peek', peek)
  const filters = view.filters ?? {}
  for (const k of VIEW_KEYS) {
    const v = filters[k]
    if (v) next.set(k, v)
  }
  if (view.group_by) next.set('group', view.group_by)
  if (view.sort) next.set('sort', view.sort)
  if (view.layout && view.layout !== 'table') next.set('layout', view.layout)
  next.set('saved', String(view.id))
  return next
}
