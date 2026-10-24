// A task list's whole body — tabs, the filter bar, saved views, the table /
// board / calendar, selection with its bulk bar, the keyboard, New task and
// the peek. The workspace Tasks page is this with tabs; an agent's Tasks tab
// is this pinned to the agent (`fixed`), so the two can never drift apart.
// Everything the list shows lives in the URL (viewState.ts), which is what
// makes a filtered list a link and a saved view a stored set of parameters.
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Plus, Search, X } from 'lucide-react'
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
import { DEFAULT_CATEGORY_NAMES, PRIORITIES, STATUS_ORDER, displayAgent, type Category } from './meta'
import { NewTaskDialog } from './NewTaskDialog'
import { SavedViews } from './SavedViews'
import { TaskBoard, movePatch } from './TaskBoard'
import { TaskCalendar } from './TaskCalendar'
import { TaskPeek } from './TaskPeek'
import { TaskTable, groupTasks, type GroupBy } from './TaskTable'
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
}

function message(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback
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

export function TasksView({ workspace, fixed, tabs = false, storageKey, onChanged }: TasksViewProps): JSX.Element {
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

  // "Due: none" has no server filter; the rest is the server's answer as is.
  const shown = useMemo(
    () => (list.tasks && state.due === 'none' ? list.tasks.filter((t) => !t.due) : list.tasks),
    [list.tasks, state.due],
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

  const patch = useCallback(
    (key: string, body: TaskPatch, optimistic?: Partial<Task>) => {
      setError(null)
      list
        .patch(key, body, optimistic)
        .then(changed)
        .catch((e: unknown) => setError(`${key}: ${message(e, 'not changed')}`))
    },
    [list, changed],
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
      if (saved.size) {
        list.setTasks((rows) => rows.map((t) => saved.get(t.key) ?? t))
        changed()
      }
    },
    [list, changed],
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
          setError(`${key}: ${message(e, 'not moved')}`)
          list.reload()
        })
    },
    [list, changed],
  )

  // ---- new task ----
  const [creating, setCreating] = useState(false)
  const [createSeq, setCreateSeq] = useState(0)
  const startCreate = useCallback(() => {
    setCreateSeq((n) => n + 1)
    setCreating(true)
  }, [])
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
        <div className="inline-flex flex-wrap items-center gap-0.5 rounded-md border border-border bg-card p-0.5" role="group" aria-label="Status">
          {CATEGORIES.map((c) => {
            const on = state.categories.includes(c)
            return (
              <button
                key={c}
                type="button"
                aria-pressed={on}
                data-testid={`category-${c}`}
                onClick={() => toggleCategory(c)}
                className={cn('rounded px-1.5 py-0.5', on ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground')}
              >
                {names?.[c] || DEFAULT_CATEGORY_NAMES[c]}
              </button>
            )
          })}
        </div>
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

      <div className="mb-3 flex flex-wrap items-center gap-1.5 text-[12px]">
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
        <SavedViews
          workspace={workspace}
          activeId={state.saved}
          current={{ filters: viewFilters(state), group_by: state.group, sort: state.sort, layout: state.layout }}
          onApply={applyView}
          onError={setError}
        />
        <Button size="xs" className="ml-auto" onClick={startCreate} title="New task (C)">
          <Plus aria-hidden="true" />
          New task
        </Button>
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
          groupBy={state.group}
          selected={selected}
          onToggle={toggle}
          focused={focused}
          names={names}
          empty="No tasks match these filters."
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
