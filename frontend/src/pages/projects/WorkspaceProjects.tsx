import { useEffect, useState, type JSX } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import { AgentApiError, getWorkspaceProject, listWorkspaceProjects, type ProjectOut } from '@/api/agents'

// WORKSPACE PROJECTS — owned by the workspace rather than one agent (a funder
// knowledge base many agents read), each kept current by a steward agent and
// addressed `<workspace>/<handle>`. Their page is the steward's project page;
// the address below is the link to share, because it survives a change of steward.

/** `/w/:workspace/projects/:handle` → the project's page on its steward's board. */
export function WorkspaceProjectRedirect(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const [target, setTarget] = useState<{ key: string; to: string | null; error: string | null } | null>(null)
  const key = `${workspace}/${handle}`

  useEffect(() => {
    let cancelled = false
    getWorkspaceProject(workspace, handle)
      .then((p) => {
        if (!cancelled)
          setTarget({ key, to: p.board_url, error: null })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const error =
          err instanceof AgentApiError && err.status === 404
            ? `No workspace project ${key}.`
            : err instanceof Error
              ? err.message
              : 'Could not load the project'
        setTarget({ key, to: null, error })
      })
    return () => {
      cancelled = true
    }
  }, [workspace, handle, key])

  const fresh = target?.key === key ? target : null
  if (fresh?.to) return <Navigate to={fresh.to} replace />
  if (fresh?.error) return <p className="p-6 text-sm text-destructive">{fresh.error}</p>
  return <p className="p-6 text-sm text-muted-foreground">Opening {key}…</p>
}

/** The workspace's shared projects, above its agents. Renders nothing when there are none. */
export function WorkspaceProjectsSection({ workspace }: { workspace: string }): JSX.Element | null {
  const [projects, setProjects] = useState<ProjectOut[] | null>(null)

  useEffect(() => {
    let cancelled = false
    listWorkspaceProjects()
      .then((rows) => {
        if (!cancelled) setProjects(rows.filter((p) => p.workspace === workspace && p.status !== 'archived'))
      })
      .catch(() => {
        if (!cancelled) setProjects([])
      })
    return () => {
      cancelled = true
    }
  }, [workspace])

  if (!projects || projects.length === 0) return null
  return (
    <section className="mb-8" aria-label="Workspace projects">
      <h2 className="text-sm font-semibold text-foreground">Workspace projects</h2>
      <p className="text-[12px] text-muted-foreground mt-0.5 mb-3">
        Shared by every agent here, each kept current by a steward.
      </p>
      <ul className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {projects.map((p) => (
          <li key={p.id}>
            <Link
              to={p.url || `/w/${workspace}/projects/${p.handle}`}
              className="group block bg-card border border-border rounded-xl p-4 hover:border-primary/40 transition-colors"
            >
              <h3 className="text-[14px] font-semibold text-foreground group-hover:text-primary truncate">{p.name}</h3>
              <p className="text-[11px] text-muted-foreground mt-0.5 font-mono truncate">{p.address}</p>
              {p.outcome && <p className="text-[12px] text-foreground-secondary mt-2 line-clamp-2">{p.outcome}</p>}
              <p className="text-[11px] text-muted-foreground mt-2">
                Steward {p.steward_slug} · {p.open_task_count} open of {p.task_count} tasks
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** The line a workspace project's page shows under its title. */
export function WorkspaceProjectLine({ project }: { project: ProjectOut }): JSX.Element | null {
  if (project.scope !== 'workspace') return null
  return (
    <p className="text-[12px] text-muted-foreground mt-1" data-testid="workspace-project-line">
      Workspace project <span className="font-mono">{project.address}</span> · kept current by{' '}
      {project.steward_slug} · any agent in the workspace can file tasks into it
    </p>
  )
}
