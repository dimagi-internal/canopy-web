// A task list's whole body — tabs, the filter bar, saved views, the table /
// board / calendar, selection with its bulk bar, the keyboard, New task and
// the peek. The workspace Tasks page is this with tabs; an agent's Tasks tab
// is this pinned to the agent (`fixed`), and a project page's Tasks section is
// this pinned to the project, so the three can never drift apart.
// Everything the list shows lives in the URL (viewState.ts), which is what
// makes a filtered list a link and a saved view a stored set of parameters.
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ChevronDown, ChevronRight, ListFilter, Plus, Search, X } from 'lucide-react'
import { cn } from 'canopy-ui/lib'
import { Button, Segmented } from 'canopy-ui/ui'

import {
  countTasks,
  getTaskSettings,
  listTasks,
  type BulkResult,
  type SavedView,
  type Task,
  type TaskPatch,
  type TaskQuery,
  type TaskStatus,
} from '@/api/work'
import { describeSelection } from '@/widget/pageState'
import { usePageState } from '@/widget/usePageState'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

import { BulkBar } from './BulkBar'
import { DEFAULT_CATEGORY_NAMES, PRIORITIES, STATUS_CATEGORY, STATUS_ORDER, displayAgent, type Category } from './meta'
import { NewTaskDialog } from './NewTaskDialog'
import { SavedViews } from './SavedViews'
import { TaskBoard, movePatch } from './TaskBoard'
import { TaskCalendar } from './TaskCalendar'
import { TaskPeek } from './TaskPeek'
import { ALL_COLUMNS as TABLE_COLUMNS, TaskTable, groupTasks, type GroupBy } from './TaskTable'
import { useDirectory } from './useDirectory'
import { usePeek } from './usePeek'
import { useTaskKeys } from './useTaskKeys'
import { useTaskList } from './useTaskList'
import {
  ALL_TABS,
  CATEGORIES,
  GROUPS,
  SORTS,
  TABS,
  normalizeParams,
  parseView,
  setCategories,
  toQuery,
  viewFilters,
  viewParams,
  type Layout,
  type TabId,
} from './viewState'
import { errorText } from '../../api/problem'

const SELECT = 'h-7 rounded border border-input bg-input px-1.5 text-[12px] text-foreground'
const COUNT_KEYS = ['agent', 'project', 'status', 'waiting', 'assignee', 'created_by', 'label', 'priority', 'category', 'kind'] as const
const COUNT_DEBOUNCE_MS = 800

const LAYOUTS: { value: Layout; label: string }[] = [
  { value: 'table', label: 'Table' },
  { value: 'board', label: 'Board' },
  { value: 'calendar', label: 'Calendar' },
]

export interface TasksViewProps {
  workspace: string
  /** Filters the caller pins (e.g. `{ agent: 'eva' }`); they override the URL's. */
  fixed?: TaskQuery
  /** The tabs to offer: `true` for all four, or a list (the first is the default). */
  tabs?: boolean | readonly TabId[]
  /** Remember the last list this page showed (per viewer, in this browser). */
  storageKey?: string
  /** After any change this list makes to a task (a rail badge re-reads). */
  onChanged?: () => void
  /** Indent subtasks under their parent in the table (a project's list). */
  nest?: boolean
  /** Offer New task (the button and the C key). A project page passes its `may_edit`. */
  canCreate?: boolean
  /** Show the recurring templates in their own collapsible "Repeating" group under the
   *  list (pin `recurring: 'false'` in `fixed` so the list itself is the work). */
  repeating?: boolean
}

/** A task finished in this list, kept where it was until the list is reloaded or left. */
interface Kept {
  task: Task
  /** The key of the row above it when it was finished (null: it was first). */
  after: string | null
  index: number
  /** Its status before, for Undo. */
  was: TaskStatus
}

const closed = (status: TaskStatus) => {
  const c = STATUS_CATEGORY[status]
  return c === 'completed' || c === 'canceled'
}

function readStored(key: string | undefined): string | null {
  if (!key) return null
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStored(key: string | undefined, value: string): void {
  if (!key) return
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Storage is a convenience; a private window simply forgets.
  }
}

export function TasksView({
  workspace,
  fixed,
  tabs = false,
  storageKey,
  onChanged,
  nest = false,
  canCreate = true,
  repeating = false,
}: TasksViewProps): JSX.Element {
  const [params, setParams] = useSearchParams()
  const tabList = useMemo<TabId[]>(() => (tabs === true ? ALL_TABS : Array.isArray(tabs) ? [...tabs] : []), [tabs])
  const tabKey = tabList.join(',')
  const fixedKey = JSON.stringify(fixed ?? {})
  // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by value, not identity
  const pinned = useMemo<TaskQuery>(() => fixed ?? {}, [fixedKey])

  // eslint-disable-next-line react-hooks/exhaustive-deps -- tabList is keyed by tabKey
  const state = useMemo(() => parseView(params, tabList), [params, tabKey])
  const query = useMemo(() => toQuery(state, pinned), [state, pinned])
  const queryKey = JSON.stringify(query)

  const update = useCallback(
    (edit: (next: URLSearchParams) => void) =>
      setParams(
        (prev) => {
          const next = normalizeParams(prev)
          edit(next)
          return next
        },
        { replace: true },
      ),
    [setParams],
  )

  // A page opened bare comes back to what it last showed.
  const restored = useRef(false)
  useEffect(() => {
    if (restored.current) return
    restored.current = true
    const bare = [...params.keys()].every((k) => k === 'peek')
    const stored = readStored(storageKey)
    if (bare && stored) update((next) => new URLSearchParams(stored).forEach((v, k) => k !== 'peek' && next.set(k, v)))
  }, [params, storageKey, update])
  useEffect(() => {
    const keep = new URLSearchParams(params)
    keep.delete('peek')
    writeStored(storageKey, keep.toString())
  }, [params, storageKey])

  const list = useTaskList(() => listTasks(query, workspace), [workspace, queryKey])
  const directory = useDirectory(workspace)
  const { peek, open } = usePeek()
  const [names, setNames] = useState<Record<string, string> | null>(null)
  useEffect(() => {
    let cancelled = false
    getTaskSettings(workspace)
      .then((s) => !cancelled && setNames({ ...s.status_names }))
      .catch(() => !cancelled && setNames(null))
    return () => {
      cancelled = true
    }
  }, [workspace])

  // ---- done stays in place ----
  // A task finished here (or canceled) would drop out of an open-work list on the next
  // re-read, and a mis-tap could not be undone where it happened. It stays in its row,
  // struck through, with Undo, until the list's own query changes or the page is left.
  const [kept, setKept] = useState<Map<string, Kept>>(() => new Map())
  useEffect(() => setKept(new Map()), [queryKey, workspace])

  // "Due: none" has no server filter; the rest is the server's answer as is.
  const fetched = useMemo(
    () => (list.tasks && state.due === 'none' ? list.tasks.filter((t) => !t.due) : list.tasks),
    [list.tasks, state.due],
  )
  const shown = useMemo(() => {
    if (!fetched || kept.size === 0) return fetched
    const out = [...fetched]
    for (const [key, k] of kept) {
      if (out.some((t) => t.key === key)) continue
      const above = k.after ? out.findIndex((t) => t.key === k.after) : -1
      out.splice(above >= 0 ? above + 1 : Math.min(k.index, out.length), 0, k.task)
    }
    return out
  }, [fetched, kept])
  const keptKeys = useMemo(
    () => new Set([...kept.values()].filter((k) => closed(k.task.status)).map((k) => k.task.key)),
    [kept],
  )

  // My tasks is the default, but an empty one on a first visit is a dead end:
  // show everything instead, once.
  const fellBack = useRef(false)
  useEffect(() => {
    if (fellBack.current || list.tasks === null) return
    fellBack.current = true
    if (state.tab === 'mine' && !state.tabExplicit && tabList.includes('all') && list.tasks.length === 0) {
      update((next) => next.set('tab', 'all'))
    }
  }, [list.tasks, state.tab, state.tabExplicit, tabList, update])

  // ---- counts in the tab labels ----
  const [counts, setCounts] = useState<Partial<Record<TabId, number>>>({})
  const [tick, setTick] = useState(0)
  const countTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const bumpCounts = useCallback(() => {
    if (countTimer.current) clearTimeout(countTimer.current)
    countTimer.current = setTimeout(() => setTick((n) => n + 1), COUNT_DEBOUNCE_MS)
  }, [])
  useEffect(() => () => {
    if (countTimer.current) clearTimeout(countTimer.current)
  }, [])
  useResource(TASK_RESOURCE, bumpCounts)
  const categoryKey = state.categories.join(',')
  useEffect(() => {
    let cancelled = false
    for (const tab of tabList) {
      const full: Record<string, unknown> = { ...TABS.find((t) => t.id === tab)?.query, ...pinned }
      if (categoryKey) full.category = categoryKey
      const q = Object.fromEntries(COUNT_KEYS.filter((k) => full[k] !== undefined).map((k) => [k, String(full[k])]))
      countTasks(q, workspace)
        .then((n) => !cancelled && setCounts((c) => ({ ...c, [tab]: n })))
        .catch(() => undefined)
    }
    return () => {
      cancelled = true
    }
  }, [tabList, pinned, categoryKey, workspace, tick])

  const [error, setError] = useState<string | null>(null)
  const changed = useCallback(() => {
    setTick((n) => n + 1)
    onChanged?.()
  }, [onChanged])

  const remember = useCallback(
    (before: Task, after: Task) => {
      const finishing = closed(after.status) && !closed(before.status)
      setKept((cur) => {
        const next = new Map(cur)
        if (finishing) {
          const rows = shown ?? []
          const i = rows.findIndex((t) => t.key === before.key)
          next.set(before.key, { task: after, after: i > 0 ? rows[i - 1].key : null, index: Math.max(i, 0), was: before.status })
        } else if (next.has(before.key)) {
          const k = next.get(before.key) as Kept
          if (closed(after.status)) next.set(before.key, { ...k, task: after })
          else next.delete(before.key)
        }
        return next
      })
    },
    [shown],
  )

  const patch = useCallback(
    (key: string, body: TaskPatch, optimistic?: Partial<Task>) => {
      setError(null)
      const before = shown?.find((t) => t.key === key)
      if (before && body.status) remember(before, { ...before, ...(optimistic ?? {}), status: body.status as TaskStatus })
      list
        .patch(key, body, optimistic)
        .then(changed)
        .catch((e: unknown) => {
          if (before && body.status) remember({ ...before, status: body.status as TaskStatus }, before)
          setError(`${key}: ${errorText(e, 'not changed')}`)
        })
    },
    [list, changed, shown, remember],
  )

  const undo = useCallback(
    (task: Task) => {
      const k = kept.get(task.key)
      if (!k) return
      // Back in the list's own rows too: the server may have dropped it already.
      list.setTasks((rows) => (rows.some((t) => t.key === task.key) ? rows : [...rows, task]))
      patch(task.key, { status: k.was }, { status: k.was })
    },
    [kept, list, patch],
  )

  // ---- the order rows are shown in (J/K, Shift-select) ----
  const order = useMemo(() => {
    const rows = shown ?? []
    if (state.layout === 'board') {
      return STATUS_ORDER.flatMap((s) => rows.filter((t) => t.status === s).map((t) => t.key))
    }
    if (state.layout === 'calendar') return rows.map((t) => t.key)
    return groupTasks(rows, state.group, names).flatMap((g) => g.tasks.map((t) => t.key))
  }, [shown, state.layout, state.group, names])

  // ---- selection ----
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const anchor = useRef<string | null>(null)
  useEffect(() => {
    setSelected(new Set())
    anchor.current = null
  }, [queryKey, workspace])
  const toggle = useCallback(
    (key: string, shiftKey: boolean) => {
      // Read the anchor now: the updater may run after it moves.
      const from = anchor.current ? order.indexOf(anchor.current) : -1
      const to = order.indexOf(key)
      setSelected((cur) => {
        const next = new Set(cur)
        if (shiftKey && from >= 0 && to >= 0) {
          const [a, b] = from < to ? [from, to] : [to, from]
          for (const k of order.slice(a, b + 1)) next.add(k)
        } else if (next.has(key)) next.delete(key)
        else next.add(key)
        return next
      })
      anchor.current = key
    },
    [order],
  )
  const selectedTasks = useMemo(() => (shown ?? []).filter((t) => selected.has(t.key)), [shown, selected])

  const onBulk = useCallback(
    (results: BulkResult[]) => {
      const saved = new Map(results.filter((r) => r.ok && r.task).map((r) => [r.key, r.task as Task]))
      for (const [key, task] of saved) {
        const before = shown?.find((t) => t.key === key)
        if (before && before.status !== task.status) remember(before, task)
      }
      if (saved.size) {
        list.setTasks((rows) => rows.map((t) => saved.get(t.key) ?? t))
        changed()
      }
    },
    [list, changed, shown, remember],
  )

  // ---- the board's drop ----
  const move = useCallback(
    (key: string, status: TaskStatus, place: { before?: string; after?: string }) => {
      const task = list.tasks?.find((t) => t.key === key)
      if (!task) return
      const body = movePatch(task, status, place)
      if (!body) return
      // Show it where it landed at once; the server's rank follows.
      list.setTasks((rows) => {
        const rest = rows.filter((t) => t.key !== key)
        const moved = { ...task, status }
        const at = place.before
          ? rest.findIndex((t) => t.key === place.before) + 1
          : place.after
            ? rest.findIndex((t) => t.key === place.after)
            : rest.length
        rest.splice(at < 0 ? rest.length : at, 0, moved)
        return rest
      })
      setError(null)
      list
        .patch(key, body, { status })
        .then(changed)
        .catch((e: unknown) => {
          setError(`${key}: ${errorText(e, 'not moved')}`)
          list.reload()
        })
    },
    [list, changed],
  )

  // ---- new task ----
  const [creating, setCreating] = useState(false)
  const [createSeq, setCreateSeq] = useState(0)
  const startCreate = useCallback(() => {
    if (!canCreate) return
    setCreateSeq((n) => n + 1)
    setCreating(true)
  }, [canCreate])
  // `?new=1` (the command palette's "New task") opens the dialog once, then leaves the URL.
  useEffect(() => {
    if (params.get('new') !== '1') return
    startCreate()
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('new')
        return next
      },
      { replace: true },
    )
  }, [params, setParams, startCreate])
  const defaults = useMemo(
    () => ({
      project: pinned.project && pinned.project.includes('/') ? pinned.project : state.project.includes('/') ? state.project : undefined,
      assignee: pinned.agent ? `agent:${pinned.agent}` : undefined,
      milestone: pinned.milestone || undefined,
    }),
    [pinned, state.project],
  )

  // ---- keyboard ----
  const [focused, setFocused] = useState<string | null>(null)
  const openKey = useCallback((key: string) => open(key), [open])
  useTaskKeys({
    order,
    focused,
    setFocused,
    onOpen: openKey,
    onToggle: toggle,
    onCreate: startCreate,
    onEscape: () => setSelected(new Set()),
    peekOpen: Boolean(peek),
    enabled: !creating,
  })

  usePageState(
    () =>
      describeSelection({
        backingTool: 'list_fleet_tasks',
        resource: TASK_RESOURCE,
        ids: order,
        filters: { ...query },
      }),
    [order, queryKey],
  )

  const onOpenTask = useCallback(
    (t: Task) => {
      setFocused(t.key)
      open(t.key)
    },
    [open],
  )

  const applyView = (view: SavedView | null) =>
    setParams((prev) => {
      if (view) return viewParams(view, prev)
      const next = normalizeParams(prev)
      next.delete('saved')
      return next
    }, { replace: true })

  const setParam = (key: string, value: string) =>
    update((next) => {
      if (value) next.set(key, value)
      else next.delete(key)
    })

  const toggleCategory = (c: Category) =>
    update((next) => {
      const on = state.categories.includes(c)
      setCategories(next, on ? state.categories.filter((x) => x !== c) : [...state.categories, c])
    })

  const show = (key: keyof TaskQuery) => !(key in pinned)
  // Below sm the dropdowns, search, grouping and layout fold into one Filter button.
  const [filtersOpen, setFiltersOpen] = useState(false)
  const activeFilters = [
    show('assignee') && state.assignee,
    show('project') && state.project,
    show('label') && state.label,
    show('priority') && state.priority,
    show('kind') && state.kind,
    state.due,
    state.q,
  ].filter(Boolean).length
  const foldable = filtersOpen ? 'flex basis-full flex-wrap items-center gap-1.5 sm:contents' : 'hidden sm:contents'

  return (
    <div className="min-w-0" data-testid="tasks-view">
      {tabList.length > 0 && (
        <div role="tablist" aria-label="Tasks" className="mb-3 flex flex-wrap gap-1 border-b border-border">
          {tabList.map((id) => {
            const tab = TABS.find((t) => t.id === id)
            if (!tab) return null
            const on = state.tab === id
            return (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={on}
                data-testid={`tab-${id}`}
                onClick={() => update((next) => next.set('tab', id))}
                className={cn(
                  '-mb-px border-b-2 px-2.5 py-1.5 text-[13px] font-medium',
                  on ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
                )}
              >
                {tab.label}
                {counts[id] !== undefined && <span className="ml-1.5 text-[12px] font-normal text-muted-foreground">{counts[id]}</span>}
              </button>
            )
          })}
        </div>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-1.5 text-[12px]" aria-label="Filters" role="group">
        <div
          className="inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-md border border-border bg-card p-0.5 sm:flex-wrap"
          role="group"
          aria-label="Status"
        >
          {CATEGORIES.map((c) => {
            const on = state.categories.includes(c)
            return (
              <button
                key={c}
                type="button"
                aria-pressed={on}
                data-testid={`category-${c}`}
                onClick={() => toggleCategory(c)}
                className={cn(
                  'shrink-0 whitespace-nowrap rounded px-1 py-0.5 sm:px-1.5',
                  on ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {names?.[c] || DEFAULT_CATEGORY_NAMES[c]}
              </button>
            )
          })}
        </div>
        <button
          type="button"
          aria-expanded={filtersOpen}
          data-testid="filters-toggle"
          onClick={() => setFiltersOpen((on) => !on)}
          className={cn(
            'inline-flex h-7 items-center gap-1 rounded border px-2 sm:hidden',
            activeFilters ? 'border-primary/40 bg-primary/10 text-primary' : 'border-input bg-input text-foreground',
          )}
        >
          <ListFilter className="size-3.5" aria-hidden="true" />
          Filter{activeFilters ? ` · ${activeFilters}` : ''}
        </button>
        {/* Last in the first row's order, so a phone has it beside Filter. */}
        {canCreate && (
          <Button size="xs" className="order-last ml-auto" onClick={startCreate} title="New task (C)">
            <Plus aria-hidden="true" />
            New task
          </Button>
        )}
        <div className={foldable} data-testid="filters-more">
        {show('assignee') && (
          <select aria-label="Assignee" value={state.assignee} onChange={(e) => setParam('assignee', e.target.value)} className={SELECT}>
            <option value="">Anyone</option>
            <option value="me">Me</option>
            <option value="none">Unassigned</option>
            {directory.agents.map((a) => (
              <option key={a.slug} value={`agent:${a.slug}`}>
                {a.name || displayAgent(a.slug)}
              </option>
            ))}
            {directory.people.map((m) => (
              <option key={m.user_id} value={m.email}>
                {m.email}
              </option>
            ))}
          </select>
        )}
        {show('project') && (
          <select aria-label="Project" value={state.project} onChange={(e) => setParam('project', e.target.value)} className={SELECT}>
            <option value="">Any project</option>
            <option value="none">No project</option>
            {directory.projects.map((p) => (
              <option key={p.id} value={p.address}>
                {p.name}
              </option>
            ))}
            {state.project && state.project !== 'none' && !directory.projects.some((p) => p.address === state.project) && (
              <option value={state.project}>{state.project}</option>
            )}
          </select>
        )}
        {show('label') && (
          <select aria-label="Label" value={state.label} onChange={(e) => setParam('label', e.target.value)} className={SELECT}>
            <option value="">Any label</option>
            {directory.labels.map((l) => (
              <option key={l.id} value={l.name}>
                {l.name}
              </option>
            ))}
            {state.label && !directory.labels.some((l) => l.name === state.label) && <option value={state.label}>{state.label}</option>}
          </select>
        )}
        {show('priority') && (
          <select aria-label="Priority" value={state.priority} onChange={(e) => setParam('priority', e.target.value)} className={SELECT}>
            <option value="">Any priority</option>
            {PRIORITIES.map((p) => (
              <option key={p.value} value={String(p.value)}>
                {p.label}
              </option>
            ))}
          </select>
        )}
        {show('kind') && (
          <select aria-label="Kind" value={state.kind} onChange={(e) => setParam('kind', e.target.value)} className={SELECT}>
            <option value="">Work and ideas</option>
            <option value="work">Work</option>
            <option value="idea">Ideas</option>
          </select>
        )}
        <select aria-label="Due" value={state.due} onChange={(e) => setParam('due', e.target.value)} className={SELECT}>
          <option value="">Any due date</option>
          <option value="overdue">Overdue</option>
          <option value="week">Due this week</option>
          <option value="none">No due date</option>
        </select>
        <SearchBox value={state.q} onChange={(q) => setParam('q', q)} />
        {state.batch && (
          <button
            type="button"
            onClick={() => setParam('batch', '')}
            title="Showing one batch — click to clear it"
            data-testid="filter-batch"
            className="inline-flex h-7 items-center gap-1 rounded border border-primary/40 bg-primary/10 px-1.5 text-primary"
          >
            Batch {state.batch} <X className="size-3" aria-hidden="true" />
          </button>
        )}
        </div>
      </div>

      <div className={cn('mb-3 flex-wrap items-center gap-1.5 text-[12px]', filtersOpen ? 'flex' : 'hidden sm:flex')}>
        <div className={foldable}>
        <select aria-label="Group by" value={state.group} onChange={(e) => setParam('group', e.target.value as GroupBy)} className={SELECT}>
          {GROUPS.map((g) => (
            <option key={g.value || 'none'} value={g.value}>
              {g.value ? `Group: ${g.label}` : g.label}
            </option>
          ))}
        </select>
        <select aria-label="Sort" value={state.sort} onChange={(e) => setParam('sort', e.target.value)} className={SELECT}>
          {SORTS.map((s) => (
            <option key={s.value || 'default'} value={s.value}>
              Sort: {s.label}
            </option>
          ))}
        </select>
        <Segmented
          label="Layout"
          size="xs"
          value={state.layout}
          options={LAYOUTS}
          onChange={(v) => setParam('layout', v === 'table' ? '' : v)}
        />
        {/* A saved view is the workspace's; one saved from a pinned project list would lose the pin. */}
        {!pinned.project && (
          <SavedViews
            workspace={workspace}
            activeId={state.saved}
            current={{ filters: viewFilters(state), group_by: state.group, sort: state.sort, layout: state.layout }}
            onApply={applyView}
            onError={setError}
          />
        )}
        </div>
      </div>

      {(error || list.error) && (
        <div role="alert" className="mb-2 flex items-start gap-2 rounded border border-destructive/30 bg-destructive/10 px-2.5 py-1.5 text-[12px] text-destructive">
          <span className="min-w-0 flex-1">{error ?? list.error}</span>
          {error && (
            <button type="button" aria-label="Dismiss" onClick={() => setError(null)} className="shrink-0 rounded p-0.5 hover:bg-destructive/10">
              <X className="size-3.5" aria-hidden="true" />
            </button>
          )}
        </div>
      )}

      {shown === null ? (
        <p className="rounded-lg border border-border bg-card px-4 py-6 text-[13px] text-muted-foreground">Loading tasks…</p>
      ) : state.layout === 'board' ? (
        <TaskBoard
          tasks={shown}
          names={names}
          onOpen={onOpenTask}
          onMove={move}
          selected={selected}
          onToggle={toggle}
          focused={focused}
        />
      ) : state.layout === 'calendar' ? (
        <TaskCalendar
          tasks={shown}
          month={state.month}
          onMonth={(m) => setParam('month', m)}
          onOpen={onOpenTask}
          focused={focused}
        />
      ) : (
        <TaskTable
          tasks={shown}
          directory={directory}
          onPatch={patch}
          onOpen={onOpenTask}
          // A list pinned to one project needs no project column.
          columns={pinned.project ? TABLE_COLUMNS.filter((c) => c !== 'project') : TABLE_COLUMNS}
          nest={nest}
          groupBy={state.group}
          selected={selected}
          onToggle={toggle}
          focused={focused}
          names={names}
          empty={repeating ? 'Nothing to do right now.' : 'No tasks match these filters.'}
          kept={keptKeys}
          onUndo={undo}
        />
      )}

      {repeating && (
        <RepeatingGroup
          workspace={workspace}
          query={{ ...pinned, recurring: 'true' }}
          directory={directory}
          names={names}
          onOpen={onOpenTask}
          onChanged={changed}
          openByDefault={shown !== null && shown.length === 0}
        />
      )}

      <BulkBar
        tasks={selectedTasks}
        directory={directory}
        names={names}
        onApplied={onBulk}
        onClear={() => setSelected(new Set())}
      />

      <NewTaskDialog
        key={createSeq}
        open={creating}
        onOpenChange={setCreating}
        workspace={workspace}
        directory={directory}
        defaults={defaults}
        names={names}
        onCreated={(task) => {
          list.reload()
          changed()
          setFocused(task.key)
          open(task.key)
        }}
      />

      <TaskPeek />
    </div>
  )
}

/** The text search: typed freely, written to the URL once typing pauses. */
function SearchBox({ value, onChange }: { value: string; onChange: (q: string) => void }): JSX.Element {
  const [draft, setDraft] = useState(value)
  const latest = useRef(onChange)
  latest.current = onChange
  useEffect(() => setDraft(value), [value])
  useEffect(() => {
    if (draft === value) return
    const t = setTimeout(() => latest.current(draft.trim()), 300)
    return () => clearTimeout(t)
  }, [draft, value])
  return (
    <label className="inline-flex h-7 items-center gap-1 rounded border border-input bg-input px-1.5 text-muted-foreground">
      <Search className="size-3.5" aria-hidden="true" />
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') latest.current(draft.trim())
          if (e.key === 'Escape') setDraft(value)
        }}
        placeholder="Search"
        aria-label="Search tasks"
        className="w-36 bg-transparent text-[12px] text-foreground placeholder:text-placeholder focus:outline-none"
      />
    </label>
  )
}

/** A project's recurring templates — its chores, say — under the work list: each says how
 *  often it repeats and when it was last done. Folded by default while there is work. */
function RepeatingGroup({
  workspace,
  query,
  directory,
  names,
  onOpen,
  onChanged,
  openByDefault,
}: {
  workspace: string
  query: TaskQuery
  directory: ReturnType<typeof useDirectory>
  names: Record<string, string> | null
  onOpen: (task: Task) => void
  onChanged: () => void
  openByDefault: boolean
}): JSX.Element | null {
  const queryKey = JSON.stringify(query)
  const list = useTaskList(() => listTasks(query, workspace), [workspace, queryKey])
  const [open, setOpen] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const isOpen = open ?? openByDefault
  const tasks = list.tasks
  if (!tasks || tasks.length === 0) return null
  const Chevron = isOpen ? ChevronDown : ChevronRight
  return (
    <div className="mt-4" data-testid="repeating">
      <button
        type="button"
        aria-expanded={isOpen}
        onClick={() => setOpen(!isOpen)}
        className="mb-2 inline-flex items-center gap-1 text-[13px] font-semibold text-foreground hover:text-primary"
      >
        <Chevron className="size-4" aria-hidden="true" />
        Repeating <span className="font-normal text-muted-foreground">{tasks.length}</span>
      </button>
      {error && <p role="alert" className="mb-2 text-[12px] text-destructive">{error}</p>}
      {isOpen && (
        <TaskTable
          tasks={tasks}
          directory={directory}
          onPatch={(key, body, optimistic) => {
            setError(null)
            list
              .patch(key, body, optimistic)
              .then(onChanged)
              .catch((e: unknown) => setError(`${key}: ${errorText(e, 'not changed')}`))
          }}
          onOpen={onOpen}
          columns={TABLE_COLUMNS.filter((c) => c !== 'project' && c !== 'due')}
          names={names}
        />
      )}
    </div>
  )
}
