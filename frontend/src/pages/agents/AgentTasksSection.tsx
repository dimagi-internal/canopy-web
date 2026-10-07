import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'

import {
  listProjects,
  listTaskActions,
  listTasks,
  type ProjectOut,
  type TaskActionOut,
  type TaskFilters,
  type TaskOut,
} from '@/api/agents'
import { TaskCard, TasksBoard } from '@/components/TasksBoard'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { ProjectGroupHeader } from '@/pages/agents/projectParts'
import { QuickTurn } from '@/pages/agents/QuickTurn'
import { describeSelection } from '@/widget/pageState'
import { usePageState } from '@/widget/usePageState'
import { useResource } from '@/widget/useResource'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

// The resource canopy marks dirty whenever any task row moves
// (`apps/harness/signals.py::ITEM_RESOURCE`). The string is the server's, so it
// is the one this page listens on.
const TASK_RESOURCE = 'item://'

type View = 'waiting' | 'open' | 'done'

// What the page is showing IS the URL: `?waiting=me`, `?view=done`,
// `?project=P2|none`, `?by=project`. A link to "what is waiting on me" is a link
// to this page, which is why the old Inbox address redirects here.
function filtersFor(view: View, project: string): TaskFilters {
  const f: TaskFilters =
    view === 'waiting'
      ? { waiting: 'me' }
      : { status: view === 'done' ? 'done,declined' : 'suggested,in_progress' }
  if (project) f.project = project
  return f
}

// EVERYTHING THIS AGENT HAS ON ITS PLATE — the board, plus one filter bar.
//
// It replaces Work and Inbox. Inbox was this same list filtered to what waits
// on you, rendered with a second card (ItemCard) that offered a different set
// of buttons for the same row; now it is a chip on this page and the card is
// the one card. Grouping by project is a view of the same rows, not a
// destination — a project's own page is on Projects.
export function AgentTasksSection(): JSX.Element {
  const { agent, canEdit, waiting, refreshWaiting } = useOutletContext<AgentOutletContext>()
  const [params, setParams] = useSearchParams()

  const view: View =
    params.get('waiting') === 'me' ? 'waiting' : params.get('view') === 'done' ? 'done' : 'open'
  const project = params.get('project') ?? ''
  const byProject = params.get('by') === 'project'

  const filters = useMemo(() => filtersFor(view, project), [view, project])
  // Stamp what we hold with what it answers, so a filter change shows the
  // skeleton rather than the previous filter's rows under the new chip.
  const key = `${agent.slug}|${JSON.stringify(filters)}`

  const [data, setData] = useState<{
    key: string
    tasks: TaskOut[]
    actions: TaskActionOut[]
  } | null>(null)
  const [projects, setProjects] = useState<ProjectOut[]>([])

  const reload = useCallback(() => {
    let cancelled = false
    const slug = agent.slug
    const stamp = `${slug}|${JSON.stringify(filters)}`
    void Promise.all([
      listTasks(slug, filters).catch(() => [] as TaskOut[]),
      listTaskActions(slug).catch(() => [] as TaskActionOut[]),
    ]).then(([tasks, actions]) => {
      if (!cancelled) setData({ key: stamp, tasks, actions })
    })
    return () => {
      cancelled = true
    }
  }, [agent.slug, filters])

  useEffect(() => reload(), [reload])

  // The project list feeds the filter and the group headers; it does not
  // depend on the filters, so it is not refetched with them.
  useEffect(() => {
    let cancelled = false
    listProjects(agent.slug)
      .then((rows) => !cancelled && setProjects(rows))
      .catch(() => !cancelled && setProjects([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  // An action moves a task, and it may move it out of "waiting on you" — so the
  // rail badge and the chip re-read too.
  const onChanged = useCallback(() => {
    reload()
    refreshWaiting()
  }, [reload, refreshWaiting])

  useResource(TASK_RESOURCE, onChanged)

  const fresh = data?.key === key ? data : null
  const tasks = fresh?.tasks ?? null
  const actions = fresh?.actions ?? []

  // `list_fleet_tasks`, not the per-agent `list_tasks`: this page's grant
  // (`agent.tasks` → `tasks:read`, apps/tokens/self_host.py) reaches only that
  // tool, and its `agent` filter narrows it to exactly these rows.
  usePageState(
    () =>
      describeSelection({
        backingTool: 'list_fleet_tasks',
        resource: TASK_RESOURCE,
        ids: (tasks ?? []).map((t) => t.ext_id),
        filters: { agent: agent.slug, ...filters },
      }),
    [tasks, agent.slug, filters],
  )

  const update = (edit: (next: URLSearchParams) => void) => {
    const next = new URLSearchParams(params)
    edit(next)
    setParams(next, { replace: true })
  }

  const chooseView = (v: View) =>
    update((next) => {
      next.delete('waiting')
      next.delete('view')
      if (v === 'waiting') next.set('waiting', 'me')
      if (v === 'done') next.set('view', 'done')
    })

  // Grouped and narrowed to one project: one group. Narrowed to "No project":
  // only the loose group.
  const groups =
    project === 'none' ? [] : project ? projects.filter((p) => p.ext_id === project) : projects

  const chips: Array<{ view: View; label: string }> = [
    { view: 'waiting', label: waiting === undefined ? 'Waiting on you' : `Waiting on you · ${waiting}` },
    { view: 'open', label: 'Open' },
    { view: 'done', label: 'Done' },
  ]

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Tasks" count={tasks?.length} />

      <div className="-mt-2 mb-6 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12px]">
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Show">
          {chips.map((chip) => (
            <button
              key={chip.view}
              type="button"
              aria-pressed={view === chip.view}
              onClick={() => chooseView(chip.view)}
              data-testid={`filter-${chip.view}`}
              className={`min-h-11 rounded-full border px-3 py-1 sm:min-h-8 ${
                view === chip.view
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border text-foreground hover:bg-muted'
              }`}
            >
              {chip.label}
            </button>
          ))}
        </div>

        <div className="inline-flex items-center gap-1.5 text-muted-foreground">
          <label htmlFor="tasks-project-filter">Project</label>
          <select
            id="tasks-project-filter"
            value={project}
            onChange={(e) => update((next) => {
              if (e.target.value) next.set('project', e.target.value)
              else next.delete('project')
            })}
            className="min-h-11 rounded border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-8"
          >
            <option value="">All</option>
            {projects.map((p) => (
              <option key={p.ext_id} value={p.ext_id}>
                {p.ext_id} · {p.name}
              </option>
            ))}
            <option value="none">No project</option>
          </select>
        </div>

        <label className="inline-flex min-h-11 items-center gap-1.5 text-muted-foreground sm:min-h-0">
          <input
            type="checkbox"
            checked={byProject}
            onChange={(e) => update((next) => {
              if (e.target.checked) next.set('by', 'project')
              else next.delete('by')
            })}
          />
          Group by project
        </label>
      </div>

      {/* Dispatching work belongs with the work. */}
      <div className="mb-6"><QuickTurn slug={agent.slug} /></div>

      {tasks === null ? (
        <WorkbenchSkeleton />
      ) : byProject ? (
        <ByProject
          slug={agent.slug}
          projects={groups}
          tasks={tasks}
          actions={actions}
          canEdit={canEdit}
          onChanged={onChanged}
        />
      ) : (
        <TasksBoard tasks={tasks} actions={actions} onChanged={onChanged} canEdit={canEdit} />
      )}
    </div>
  )
}

function ByProject({
  slug,
  projects,
  tasks,
  actions,
  canEdit,
  onChanged,
}: {
  slug: string
  projects: ProjectOut[]
  tasks: TaskOut[]
  actions: TaskActionOut[]
  canEdit: boolean
  onChanged: () => void
}): JSX.Element {
  // Tasks with no project are not an error — most tasks start that way — so
  // they get a group rather than disappearing from a view of "all the work".
  const loose = tasks.filter((t) => !t.project_ext_id)
  // `actions` arrives newest-first: the first applied one per task is its latest.
  const lastByTask = useMemo(() => {
    const m = new Map<string, TaskActionOut>()
    for (const a of actions) {
      if (a.status === 'applied' && a.task_ext_id && !m.has(a.task_ext_id)) m.set(a.task_ext_id, a)
    }
    return m
  }, [actions])

  const cards = (list: TaskOut[]) => (
    <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
      {list.map((t) => (
        <TaskCard
          key={t.ext_id}
          task={t}
          onChanged={onChanged}
          canEdit={canEdit}
          lastApplied={lastByTask.get(t.ext_id)}
        />
      ))}
    </div>
  )

  return (
    <div className="space-y-6">
      {projects.map((p) => {
        const mine = tasks.filter((t) => t.project_ext_id === p.ext_id)
        return (
          <section key={p.ext_id} data-testid={`project-${p.ext_id}`}>
            <ProjectGroupHeader project={p} tasks={mine} slug={slug} onChanged={onChanged} />
            {mine.length === 0 ? (
              <p className="mt-2 text-[12px] text-muted-foreground">No tasks here.</p>
            ) : (
              cards(mine)
            )}
          </section>
        )
      })}

      {loose.length > 0 && (
        <section data-testid="project-none">
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            No project
          </h3>
          {cards(loose)}
        </section>
      )}

      {projects.length === 0 && loose.length === 0 && (
        <p className="text-[13px] text-muted-foreground">No tasks here.</p>
      )}
    </div>
  )
}
