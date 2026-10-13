import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'

import { AgentApiError, getProject, patchProject, type ProjectDetailOut, type TaskOut } from '@/api/agents'
import { relativeTime } from '@/components/activity/turnLog'
import { nestTasks, TaskCard } from '@/components/TasksBoard'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { ProjectOverview } from '@/pages/agents/ProjectOverview'
import { ProjectStatusOptions, StatusChip } from '@/pages/agents/projectParts'
import { Section } from '@/pages/agents/sectionLayout'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'
import { WorkbenchSkeleton } from 'canopy-ui'

// ONE PROJECT'S PAGE — the one page for everyone, teammate or editor, always
// in the same order: what it is, where it stands, the ideas, the people
// (ProjectOverview), then the agent's work on it and the turns that touched it.
// The link to share is this one; the old team-view address redirects here.

/** Live tasks first; otherwise the server's order (position) stands. */
function liveFirst(tasks: readonly TaskOut[]): TaskOut[] {
  const live = (t: TaskOut) => t.status === 'suggested' || t.status === 'in_progress'
  return [...tasks.filter(live), ...tasks.filter((t) => !live(t))]
}

export function AgentProjectPage(): JSX.Element {
  // `canEdit` is the workspace shell's one answer to "may this person dispatch
  // or close a task" — the same value the Tasks board hands its cards — and
  // `refreshWaiting` keeps the rail's "waiting on you" badge honest after a card
  // acts.
  const { agent, canEdit, refreshWaiting } = useOutletContext<AgentOutletContext>()
  const { ref = '', workspace = '' } = useParams<{
    ref: string
    workspace: string
  }>()

  const [data, setData] = useState<{
    key: string
    detail: ProjectDetailOut | null
    error: string | null
  } | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const key = `${agent.slug}/${ref}`

  const reload = useCallback(() => {
    let cancelled = false
    const k = `${agent.slug}/${ref}`
    getProject(agent.slug, ref)
      .then((detail) => {
        if (!cancelled) setData({ key: k, detail, error: null })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const error =
          err instanceof AgentApiError && err.status === 404
            ? `No project ${ref} on ${agent.name || agent.slug}.`
            : err instanceof Error
              ? err.message
              : 'Could not load the project'
        setData({ key: k, detail: null, error })
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug, agent.name, ref])

  useEffect(() => reload(), [reload])
  // A task on this project moved (here, on the Tasks page, or by the agent).
  useResource(TASK_RESOURCE, () => {
    reload()
  })

  const fresh = data?.key === key ? data : null
  const detail = fresh?.detail ?? null

  const back = (
    <Link to=".." relative="path" className="text-[12px] text-muted-foreground hover:text-foreground">
      ← Projects
    </Link>
  )

  if (!fresh) {
    return (
      <div className="max-w-4xl px-6 py-8">
        {back}
        <WorkbenchSkeleton />
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="max-w-4xl px-6 py-8">
        {back}
        <p className="mt-4 text-[13px] text-destructive">{fresh.error}</p>
      </div>
    )
  }

  const tasks = liveFirst(detail.tasks ?? [])
  const nested = nestTasks(tasks)
  const turns = detail.recent_turns ?? []
  const owner = detail.owner_email || detail.owner_note
  const pageHref = (personId: number) =>
    `/w/${workspace}/agents/${agent.slug}/projects/${encodeURIComponent(detail.ext_id)}/people/${personId}`

  // The project's own fields, for whoever runs it; a teammate sees them read-only.
  const details = (
    <>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12px] text-muted-foreground">
        <span>{detail.ext_id}</span>
        <StatusChip status={detail.status} />
        {canEdit && (
          <label className="inline-flex items-center gap-1.5">
            Status
            <select
              aria-label="Project status"
              value={detail.status}
              onChange={(e) => {
                const status = e.target.value
                setStatusError(null)
                void patchProject(agent.slug, detail.ext_id, { status })
                  .then(() => reload())
                  .catch((err: unknown) =>
                    setStatusError(
                      err instanceof AgentApiError && err.detail
                        ? err.detail
                        : err instanceof Error
                          ? err.message
                          : 'Could not change the status',
                    ),
                  )
              }}
              className="min-h-11 rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground sm:min-h-0"
            >
              <ProjectStatusOptions />
            </select>
          </label>
        )}
        {canEdit && owner && <span>Owner: {owner}</span>}
        {detail.drive_folder_url && (
          <a href={detail.drive_folder_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            Drive folder
          </a>
        )}
        {detail.repo_slug && <span>Repo: {detail.repo_slug}</span>}
      </div>
      {statusError && <p className="mt-2 text-[12px] text-destructive">{statusError}</p>}
    </>
  )
  const who = agent.name || agent.slug

  return (
    // pb-28: room under the last section for the site-wide floating "Canopy AI" button.
    <div className="max-w-4xl px-6 py-8 pb-28">
      {back}
      <div className="mt-4">
        <ProjectOverview
          agent={agent.slug}
          projectRef={detail.ext_id}
          pageHref={pageHref}
          showRefs={canEdit}
          details={details}
        />
      </div>

      {/* The agent's working detail is for whoever runs the work. A teammate
          reads the overview above: task cards, approve buttons and review
          logs read to them as an admin console. */}
      {canEdit && (
        <>
          <Section
            id="project-tasks"
            title={`${who}'s work`}
            description={`The tasks ${who} is doing on this project, open first. An idea's subtasks are the work toward it.`}
          >
            {tasks.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">No tasks in this project yet.</p>
            ) : (
              <div className="space-y-2">
                {nested.roots.map((t) => (
                  <TaskCard
                    key={t.ext_id}
                    task={t}
                    onChanged={() => {
                      reload()
                      refreshWaiting()
                    }}
                    canEdit={canEdit}
                    // Every card on this page is in this project.
                    showProject={false}
                    // A product area is one board: its big pieces nest their subtasks.
                    childrenOf={nested.childrenOf}
                  />
                ))}
              </div>
            )}
          </Section>

          <Section
            id="project-activity"
            title="Activity"
            description="Recent turns that touched one of this project's tasks, newest first."
          >
            {turns.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">No turns have touched this project yet.</p>
            ) : (
              <ul className="divide-y divide-border rounded-lg border border-border">
                {turns.map((turn) => (
                  <li key={turn.id}>
                    <Link
                      to={`../../turns#${turn.id}`}
                      relative="path"
                      data-testid={`turn-${turn.id}`}
                      className="flex items-baseline gap-2 px-3 py-2 text-[13px] hover:bg-muted"
                    >
                      <span className="shrink-0 text-[11px] text-muted-foreground">{turn.status}</span>
                      <span className="min-w-0 flex-1 truncate text-foreground">
                        {turn.prompt_preview || '(no prompt)'}
                      </span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">
                        {relativeTime(turn.created_at, new Date())}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      )}
    </div>
  )
}
