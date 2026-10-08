import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'

import { AgentApiError, getProject, patchProject, type ProjectDetailOut, type TaskOut } from '@/api/agents'
import { relativeTime } from '@/components/activity/turnLog'
import { TaskCard } from '@/components/TasksBoard'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { ProjectStatusOptions, StatusChip } from '@/pages/agents/projectParts'
import { Section } from '@/pages/agents/sectionLayout'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'
import { WorkbenchSkeleton } from 'canopy-ui'

// ONE PROJECT — always the same four sections in the same order (Header, Tasks,
// Activity, Links), so a person learns one page rather than one per project.

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
  const { ref = '' } = useParams<{ ref: string }>()

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
  const turns = detail.recent_turns ?? []
  const links = detail.links ?? []
  const owner = detail.owner_email || detail.owner_note

  return (
    <div className="max-w-4xl px-6 py-8">
      {back}

      {/* Header */}
      <header className="mt-3 mb-8 border-b border-border pb-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-muted-foreground">{detail.ext_id}</span>
          <h1 className="text-base font-semibold text-foreground">{detail.name}</h1>
          <StatusChip status={detail.status} />
        </div>
        {detail.outcome && <p className="mt-1 text-[13px] text-foreground-secondary">{detail.outcome}</p>}
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12px] text-muted-foreground">
          <label className="inline-flex items-center gap-1.5">
            Status
            <select
              aria-label="Project status"
              disabled={!canEdit}
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
          {owner && <span>Owner: {owner}</span>}
          {detail.drive_folder_url && (
            <a
              href={detail.drive_folder_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              Drive folder
            </a>
          )}
          {detail.repo_slug && <span>Repo: {detail.repo_slug}</span>}
        </div>
        {statusError && <p className="mt-2 text-[12px] text-destructive">{statusError}</p>}
      </header>

      <Section id="project-tasks" title="Tasks" description="This project's tasks, open first.">
        {tasks.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No tasks in this project yet.</p>
        ) : (
          <div className="space-y-2">
            {tasks.map((t) => (
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

      <Section id="project-links" title="Links" description="Deliverables the agent recorded on this project.">
        <details data-testid="project-links">
          <summary className="cursor-pointer text-[12px] text-muted-foreground hover:text-foreground">
            {links.length} link{links.length === 1 ? '' : 's'}
          </summary>
          {links.length > 0 && (
            <ul className="mt-2 space-y-1 text-[13px]">
              {links.map((l) => (
                <li key={`${l.label}|${l.url}`}>
                  <a href={l.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                    {l.label || l.url}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </details>
      </Section>
    </div>
  )
}
