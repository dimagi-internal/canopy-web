import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, useOutletContext } from 'react-router-dom'

import { listProjects, type ProjectOut } from '@/api/agents'
import { relativeTime } from '@/components/activity/turnLog'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { NewProject, StatusChip } from '@/pages/agents/projectParts'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

// THE AGENT'S PROJECTS — its landing page. A project is a piece of work with an
// end: an outcome, an owner, a Drive folder, and the tasks that get it there.
// One row per project, live ones first; finished ones fold into a group below
// because they are history you ask for, not what this page opens on.
export function AgentProjectsSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()

  // Stamped with the slug it belongs to, so switching agents never shows the
  // previous agent's rows while the next list loads.
  const [data, setData] = useState<{ slug: string; projects: ProjectOut[]; error: string | null } | null>(null)

  const reload = useCallback(() => {
    let cancelled = false
    const slug = agent.slug
    listProjects(slug)
      .then((projects) => {
        if (!cancelled) setData({ slug, projects, error: null })
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setData({ slug, projects: [], error: err instanceof Error ? err.message : 'Could not load projects' })
        }
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  useEffect(() => reload(), [reload])

  const fresh = data?.slug === agent.slug ? data : null
  const projects = fresh?.projects ?? []
  const active = projects.filter((p) => p.status === 'active')
  const finished = projects.filter((p) => p.status !== 'active')

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Projects" count={fresh ? active.length : undefined} />

      <div className="mb-6">
        <NewProject slug={agent.slug} onCreated={() => void reload()} />
      </div>

      {!fresh ? (
        <WorkbenchSkeleton />
      ) : fresh.error ? (
        <p className="text-[13px] text-destructive">{fresh.error}</p>
      ) : projects.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          No projects yet. A project is a piece of work with an end — add one above.
        </p>
      ) : (
        <>
          <div className="space-y-2">
            {active.map((p) => (
              <ProjectRow key={p.ext_id} project={p} />
            ))}
          </div>
          {finished.length > 0 && (
            <details className="mt-6" data-testid="projects-finished">
              <summary className="cursor-pointer text-[12px] text-muted-foreground hover:text-foreground">
                Done and archived ({finished.length})
              </summary>
              <div className="mt-2 space-y-2">
                {finished.map((p) => (
                  <ProjectRow key={p.ext_id} project={p} />
                ))}
              </div>
            </details>
          )}
        </>
      )}
    </div>
  )
}

function ProjectRow({ project: p }: { project: ProjectOut }): JSX.Element {
  // The API counts open tasks per project but not waiting ones, so the row
  // shows "N open" only; the project page carries the full picture.
  const meta = [
    p.owner_email || p.owner_note,
    `${p.open_task_count} open`,
    relativeTime(p.updated_at, new Date()),
  ].filter(Boolean)

  return (
    <Link
      to={p.ext_id}
      data-testid={`project-row-${p.ext_id}`}
      className="block rounded-lg border border-border p-3 hover:bg-muted"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted-foreground">{p.ext_id}</span>
        <span className="text-sm font-medium text-foreground">{p.name}</span>
        <StatusChip status={p.status} />
        <span className="ml-auto text-[11px] text-muted-foreground">{meta.join(' · ')}</span>
      </div>
      {p.outcome && <p className="mt-1 line-clamp-1 text-[13px] text-foreground-secondary">{p.outcome}</p>}
    </Link>
  )
}
