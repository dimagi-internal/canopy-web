// /w/:workspace/projects/:handle — a project's ONE page, for teammates and
// editors alike. In order: its properties (status, health, lead, lead agent
// and the other agents, dates, priority, outcome, where its files are); the
// latest update, or a nudge when one is due, and the form to post one; its
// milestones; scope against done; every task in it; then what it proposes, its
// ideas and people, its check-ins and the turns that touched it. A teammate
// sees all of it; the controls they may not use (`may_edit`) are not drawn.
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { X } from 'lucide-react'
import { Button } from 'canopy-ui/ui'
import { WorkbenchSkeleton } from 'canopy-ui'
import {
  createTask,
  getProjectByAddress,
  listTasks,
  updateProject,
  type ProjectDetail,
  type ProjectPatch,
} from '@/api/work'
import { ProjectViewsSection } from '@/dynamicViews/ProjectViewsSection'
import { Section } from '@/components/objectPage/settingsLayout'
import { ProposalCard } from '@/components/projects/ProposalCard'
import { useEngagementView } from '@/components/projects/useEngagementView'
import { ProgressBar } from '@/components/work/projectBits'
import { ScopeChart, scopePoints } from '@/components/work/ScopeChart'
import { TaskPeek } from '@/components/work/TaskPeek'
import { TaskTable } from '@/components/work/TaskTable'
import { useDirectory } from '@/components/work/useDirectory'
import { usePeek } from '@/components/work/usePeek'
import { useTaskList } from '@/components/work/useTaskList'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { CheckinTimeline } from '@/pages/agents/CheckinParts'
import { CheckinsPanel } from '@/pages/agents/ProjectCheckins'
import { ProjectOverview } from '@/pages/agents/ProjectOverview'
import { errorText } from '@/api/problem'
import { MilestonesSection } from '@/pages/work/ProjectMilestones'
import { PropertyHeader } from '@/pages/work/ProjectProperties'
import { ProjectServices } from '@/pages/work/ProjectServices'
import { UpdatesSection } from '@/pages/work/ProjectUpdates'
import { ProjectShares } from '@/pages/work/ProjectShares'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

/** Open work only, unless "Show done and canceled" is on. */
const OPEN_CATEGORIES = 'triage,backlog,unstarted,started'
/** How long a burst of task changes waits before the page's counts re-read. */
const REFRESH_DEBOUNCE_MS = 600

export function ProjectPage(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const address = `${workspace}/${handle}`
  const [state, setState] = useState<{ key: string; detail: ProjectDetail | null; error: string | null } | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [timelineVersion, setTimelineVersion] = useState(0)

  const reload = useCallback(() => {
    let cancelled = false
    const key = `${workspace}/${handle}`
    getProjectByAddress(workspace, handle)
      .then((detail) => !cancelled && setState({ key, detail, error: null }))
      .catch((err: unknown) => {
        if (cancelled) return
        const status = (err as { status?: number } | null)?.status
        setState({ key, detail: null, error: status === 404 ? `No project ${key}.` : errorText(err, 'Could not load the project') })
      })
    return () => {
      cancelled = true
    }
  }, [workspace, handle])
  useEffect(() => reload(), [reload])

  // A task on this project moved: its progress, milestones and turns move too.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useResource(TASK_RESOURCE, () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(reload, REFRESH_DEBOUNCE_MS)
  })
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  const fresh = state?.key === address ? state : null
  const detail = fresh?.detail ?? null
  useEngagementView(detail ? { project: detail.id, workspace: detail.workspace } : null)

  const save = useCallback(
    async (body: ProjectPatch) => {
      setSaveError(null)
      try {
        await updateProject(workspace, handle, body)
      } catch (err) {
        setSaveError(errorText(err, 'Could not save'))
      }
      reload()
    },
    [workspace, handle, reload],
  )

  const back = (
    <Link to={scopedPath(workspace, '/projects')} className="text-[12px] text-muted-foreground hover:text-foreground">
      ← Projects
    </Link>
  )

  if (!fresh) {
    return (
      <div className="max-w-5xl">
        {back}
        <WorkbenchSkeleton />
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="max-w-5xl">
        {back}
        <p className="mt-4 text-[13px] text-destructive">{fresh.error}</p>
      </div>
    )
  }

  const lead = detail.agent_slug || detail.steward_slug || ''
  // The agent-scoped routes (ideas, check-ins) know the project by its alias on its lead agent.
  const agentRef = detail.ext_id || detail.handle
  const turns = detail.recent_turns ?? []

  return (
    // pb-28: room under the last section for the site-wide floating "Canopy AI" button.
    <div className="max-w-5xl pb-28" data-testid="project-page">
      {back}
      <PropertyHeader detail={detail} workspace={workspace} onSave={save} />
      {saveError && (
        <p role="alert" className="mb-4 rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
          {saveError}
        </p>
      )}

      {/* A project's views come first: when it has one, it is usually the thing people came to use. */}
      <ProjectViewsSection workspace={workspace} handle={handle} projectName={detail.name} />
      <UpdatesSection detail={detail} workspace={workspace} handle={handle} onChanged={reload} />
      <ProjectWork detail={detail} workspace={workspace} handle={handle} address={address} onChanged={reload} />
      <ProjectServices detail={detail} workspace={workspace} handle={handle} onChanged={reload} />
      <ProjectShares detail={detail} workspace={workspace} handle={handle} onChanged={reload} />

      {detail.proposal && (
        <Section id="project-proposal" title="Proposal" description="This project was proposed. Start it or decline it here.">
          <ProposalCard proposal={detail.proposal} onChanged={reload} />
        </Section>
      )}

      {(detail.proposed_tasks ?? []).length > 0 && (
        <Section
          id="project-proposed"
          title="Proposed tasks"
          description="Tasks this project's check-ins proposed. Start one, decline it with a reason, back it or ask — the agent reads it before the next check-in."
        >
          <div className="grid gap-4 lg:grid-cols-2">
            {(detail.proposed_tasks ?? []).map((t) => (
              <ProposalCard
                key={`${t.agent}-${t.ref}`}
                task={t}
                onChanged={() => {
                  reload()
                  setTimelineVersion((v) => v + 1)
                }}
              />
            ))}
          </div>
        </Section>
      )}

      {lead && agentRef && (
        <>
          <ProjectOverview
            agent={lead}
            projectRef={agentRef}
            pageHref={(personId) =>
              scopedPath(workspace, `/agents/${lead}/projects/${encodeURIComponent(agentRef)}/people/${personId}`)
            }
            showRefs={detail.may_edit}
            bare
          />
          <Section
            id="project-checkins"
            title="Check-ins"
            description="This project's recurring meetings: who leads each one, which agents take part, the cadence, and the Slack channel it reads — only when its lead presses Confirm there."
          >
            <CheckinsPanel
              project={{ agent: lead, ref: agentRef, workspace: detail.workspace || workspace }}
              projectAgent={lead}
              canAdd={detail.may_edit}
              onRan={() => setTimelineVersion((v) => v + 1)}
            />
          </Section>
          <Section
            id="project-timeline"
            title="Timeline"
            description="Every check-in run, newest first: who took part, what was read, who confirmed it, and what it proposed."
          >
            <CheckinTimeline project={{ agent: lead, ref: agentRef, workspace: detail.workspace || workspace }} version={timelineVersion} />
          </Section>
        </>
      )}

      <Section id="project-activity" title="Recent turns" description="Agent turns that touched one of this project's tasks, newest first.">
        {turns.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No turns have touched this project yet.</p>
        ) : (
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {turns.map((turn) => (
              <li key={turn.id}>
                <Link
                  to={lead ? scopedPath(workspace, `/agents/${lead}/turns#${turn.id}`) : scopedPath(workspace, '/activity')}
                  data-testid={`turn-${turn.id}`}
                  className="flex items-baseline gap-2 px-3 py-1.5 text-[13px] hover:bg-muted/50"
                >
                  <span className="w-20 shrink-0 text-[11.5px] text-muted-foreground">{turn.status}</span>
                  <span className="min-w-0 flex-1 truncate text-foreground">{turn.prompt_preview || '(no prompt)'}</span>
                  <span className="shrink-0 text-[11.5px] text-muted-foreground">
                    {relativeAge(turn.created_at, new Date(), { seconds: false })}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <TaskPeek />
    </div>
  )
}

// ---- milestones, scope, tasks ------------------------------------------------------

/** The milestones, the graph and the task table share one filter (a milestone's tasks). */
function ProjectWork({
  detail,
  workspace,
  handle,
  address,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  address: string
  onChanged: () => void
}): JSX.Element {
  const [milestone, setMilestone] = useState<string | null>(null)
  const [showClosed, setShowClosed] = useState(false)
  const directory = useDirectory(workspace)
  const { open } = usePeek()
  const list = useTaskList(
    () => listTasks({ project: address, ...(showClosed ? {} : { category: OPEN_CATEGORIES }), limit: 500 }, workspace),
    [address, showClosed],
  )
  const [taskError, setTaskError] = useState<string | null>(null)
  const tasks = useMemo(
    () => (list.tasks ?? []).filter((t) => !milestone || t.milestone === milestone),
    [list.tasks, milestone],
  )
  const progress = detail.progress
  const points = scopePoints(progress?.scope_history)

  return (
    <>
      <MilestonesSection
        milestones={detail.milestones ?? []}
        editable={detail.may_edit}
        workspace={workspace}
        handle={handle}
        selected={milestone}
        onSelect={setMilestone}
        onChanged={onChanged}
      />

      <Section id="project-scope" title="Scope and done" description="Tasks in scope against tasks done, week by week. Canceled tasks are out of scope.">
        {progress && (
          <p className="mb-2 flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground" data-testid="project-progress">
            <ProgressBar done={progress.done} total={progress.total} />
            <span>
              {progress.done} of {progress.total} done · {progress.started} in progress
            </span>
          </p>
        )}
        <ScopeChart points={points} />
      </Section>

      <Section id="project-tasks" title="Tasks" description="Every task in this project, its subtasks under it. Open one to see it beside the list.">
        <div className="mb-2 flex flex-wrap items-center gap-3 text-[12px]">
          {milestone && (
            <span className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-primary" data-testid="milestone-filter">
              Milestone: {milestone}
              <button type="button" aria-label="Show every milestone" onClick={() => setMilestone(null)} className="hover:text-foreground">
                <X aria-hidden="true" className="size-3" />
              </button>
            </span>
          )}
          <label className="inline-flex items-center gap-1.5 text-muted-foreground">
            <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} className="accent-primary" />
            Show done and canceled
          </label>
        </div>
        {(list.error || taskError) && <p className="mb-2 text-[12px] text-destructive">{taskError || list.error}</p>}
        {list.tasks === null ? (
          <WorkbenchSkeleton />
        ) : (
          <TaskTable
            tasks={tasks}
            directory={directory}
            nest
            groupBy="status"
            columns={['labels', 'assignee', 'due', 'updated']}
            onOpen={(t) => open(t.key)}
            onPatch={(key, body, optimistic) => {
              setTaskError(null)
              list.patch(key, body, optimistic).catch((err: unknown) => setTaskError(errorText(err, 'Could not save')))
            }}
            empty={milestone ? `No ${showClosed ? '' : 'open '}tasks in ${milestone}.` : showClosed ? 'No tasks in this project yet.' : 'No open tasks.'}
          />
        )}
        {detail.may_edit && (
          <AddTask
            onAdd={async (title) => {
              await createTask({ title, project: address, workspace, status: 'todo', ...(milestone ? { milestone } : {}) }, workspace)
              list.reload()
              onChanged()
            }}
          />
        )}
      </Section>
    </>
  )
}

function AddTask({ onAdd }: { onAdd: (title: string) => Promise<void> }): JSX.Element {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <form
      className="mt-2 flex gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (!title.trim() || busy) return
        setBusy(true)
        setError(null)
        onAdd(title.trim())
          .then(() => setTitle(''))
          .catch((err: unknown) => setError(errorText(err, 'Could not add the task')))
          .finally(() => setBusy(false))
      }}
    >
      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Add a task…"
        aria-label="New task"
        className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      <Button size="sm" variant="outline" type="submit" disabled={busy || !title.trim()}>
        Add
      </Button>
      {error && <span className="self-center text-[12px] text-destructive">{error}</span>}
    </form>
  )
}
