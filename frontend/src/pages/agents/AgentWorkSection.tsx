import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'

import {
  listAgentCommands,
  listAgentProjects,
  listAgentTasks,
  type AgentCommandOut,
  type AgentProjectOut,
  type AgentTaskOut,
} from '@/api/agents'
import { TaskCard, TasksBoard } from '@/components/TasksBoard'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { ItemsSection } from '@/pages/agents/ItemsSection'
import { ProjectGroupHeader, NewProject } from '@/pages/agents/projectParts'
import { QuickTurn } from '@/pages/agents/QuickTurn'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

// WHAT THIS AGENT IS WORKING ON — one page, and the agent's landing page.
//
// It replaces three rail entries that were three renderings of ONE table:
//
//   Tasks    the board, grouped by who has the ball
//   Items    the ask ledger — `/items/` has served AgentTask rows since #873;
//            `ItemsSection` was Item-era code that was repointed, never rewritten
//   Projects the same tasks again, grouped by project, plus what a project knows
//            that a task cannot (Drive folder, outcome, open counts)
//
// The September merge (#871/#873) made an item a property of a task and moved
// every row; the UI half never landed, so the rail kept four doors onto one
// table and a task carrying an ask appeared on three of them at once. Here the
// grouping is a TOGGLE, because "how is the work organised" is a view, not a
// destination — and the ask renders on the card (see AskOnCard), so a task
// blocking on a person is visible where the work is rather than only on Inbox.
export function AgentWorkSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [params, setParams] = useSearchParams()
  const groupBy = params.get('by') === 'project' ? 'project' : 'status'
  // One sitting, e.g. a fleet audit: `?batch=` is a link somebody was handed,
  // so it keeps working and keeps showing decided rows.
  const batch = params.get('batch') ?? ''
  const showSettled = params.get('settled') === '1'

  // One state object, written only from async callbacks, and stamped with the
  // slug it belongs to: a synchronous reset inside the effect would be a
  // cascading render, and "loading" is really "what I hold is not this agent's".
  const [data, setData] = useState<{
    slug: string
    tasks: AgentTaskOut[]
    commands: AgentCommandOut[]
    projects: AgentProjectOut[]
  } | null>(null)

  const reload = useCallback(() => {
    let cancelled = false
    const slug = agent.slug
    void Promise.all([
      listAgentTasks(slug).catch(() => [] as AgentTaskOut[]),
      listAgentCommands(slug).catch(() => [] as AgentCommandOut[]),
      listAgentProjects(slug).catch(() => [] as AgentProjectOut[]),
    ]).then(([tasks, commands, projects]) => {
      if (!cancelled) setData({ slug, tasks, commands, projects })
    })
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  useEffect(() => reload(), [reload])

  const fresh = data?.slug === agent.slug ? data : null
  const tasks = fresh?.tasks ?? null
  const commands = fresh?.commands ?? []
  const projects = fresh?.projects ?? []

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params)
    if (value === null) next.delete(key)
    else next.set(key, value)
    setParams(next, { replace: true })
  }

  // Settled = done or declined. Hidden by default: the question this page opens
  // on is what is live, and a finished task is history you ask for.
  const visible = useMemo(() => {
    const rows = tasks ?? []
    return showSettled ? rows : rows.filter((t) => t.status !== 'done' && t.status !== 'declined')
  }, [tasks, showSettled])

  const settledCount = (tasks ?? []).length - (tasks ?? []).filter(
    (t) => t.status !== 'done' && t.status !== 'declined').length

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Work" count={visible.length} />

      <div className="-mt-2 mb-6 flex flex-wrap items-center gap-3 text-[12px]">
        <div className="inline-flex overflow-hidden rounded-md border border-border" role="group" aria-label="Group by">
          {(['status', 'project'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              aria-pressed={groupBy === mode}
              onClick={() => setParam('by', mode === 'status' ? null : mode)}
              data-testid={`group-by-${mode}`}
              className={`min-h-8 px-2.5 py-1 ${
                groupBy === mode ? 'bg-primary text-primary-foreground' : 'text-foreground hover:bg-muted'
              }`}
            >
              By {mode}
            </button>
          ))}
        </div>
        <label className="inline-flex items-center gap-1.5 text-muted-foreground">
          <input
            type="checkbox"
            checked={showSettled}
            onChange={(e) => setParam('settled', e.target.checked ? '1' : null)}
            data-testid="show-settled"
          />
          Show settled{settledCount > 0 ? ` (${settledCount})` : ''}
        </label>
      </div>

      {/* Dispatching work belongs with the work. This was Overview's, and
          Overview was a dashboard people had to leave to do anything. */}
      {!batch && <div className="mb-6"><QuickTurn slug={agent.slug} /></div>}

      {batch ? (
        <ItemsSection />
      ) : tasks === null ? (
        <WorkbenchSkeleton />
      ) : groupBy === 'project' ? (
        <ByProject
          slug={agent.slug}
          projects={projects}
          tasks={visible}
          commands={commands}
          onChanged={reload}
        />
      ) : (
        <TasksBoard tasks={visible} onChanged={reload} commands={commands} />
      )}
    </div>
  )
}

function ByProject({
  slug,
  projects,
  tasks,
  commands,
  onChanged,
}: {
  slug: string
  projects: AgentProjectOut[]
  tasks: AgentTaskOut[]
  commands: AgentCommandOut[]
  onChanged: () => void
}): JSX.Element {
  // Tasks with no project are not an error — most tasks start that way — so
  // they get a group rather than disappearing from a view of "all the work".
  const loose = tasks.filter((t) => !t.project_ext_id)
  const lastByTask = new Map(
    commands.filter((c) => c.status === 'applied' && c.task_id != null).map((c) => [c.task_id as number, c]),
  )

  return (
    <div className="space-y-6">
      <NewProject slug={slug} onCreated={onChanged} />

      {projects.map((project) => {
        const mine = tasks.filter((t) => t.project_ext_id === project.ext_id)
        return (
          <section key={project.ext_id} data-testid={`project-${project.ext_id}`}>
            <ProjectGroupHeader project={project} tasks={mine} slug={slug} onChanged={onChanged} />
            {mine.length === 0 ? (
              <p className="mt-2 text-[12px] text-muted-foreground">Nothing live here.</p>
            ) : (
              <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {mine.map((t) => (
                  <TaskCard key={t.id} task={t} onChanged={onChanged} lastApplied={lastByTask.get(t.id)} />
                ))}
              </div>
            )}
          </section>
        )
      })}

      {loose.length > 0 && (
        <section data-testid="project-none">
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            No project
          </h3>
          <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {loose.map((t) => (
              <TaskCard key={t.id} task={t} onChanged={onChanged} lastApplied={lastByTask.get(t.id)} />
            ))}
          </div>
        </section>
      )}

      {projects.length === 0 && loose.length === 0 && (
        <p className="text-[13px] text-muted-foreground">No work yet.</p>
      )}
    </div>
  )
}
