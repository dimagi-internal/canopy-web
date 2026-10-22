import { useEffect, useState, type JSX } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import { CopyCommand } from '@/components/agents/CopyCommand'
import { AgentApiError, getWorkspaceProject, type ProjectDetailOut, type ProjectOut } from '@/api/agents'
import { scopedPath } from '@/lib/scopedLinks'

// WORKSPACE PROJECTS — owned by the workspace rather than one agent (a funder
// knowledge base many agents read), each kept current by a steward agent and
// addressed `<workspace>/<handle>`. Their page is the steward's project page;
// the address below is the link to share, because it survives a change of steward.

/** `/w/:workspace/projects/:handle` → the project's page on its steward's board, or —
 *  for a reader who cannot open that board (a division member reading a project its
 *  parent's agent keeps) — the project itself, read-only, at its address. */
export function WorkspaceProjectRedirect(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const [target, setTarget] = useState<
    { key: string; to: string | null; project?: ProjectDetailOut; error: string | null } | null
  >(null)
  const key = `${workspace}/${handle}`

  useEffect(() => {
    let cancelled = false
    getWorkspaceProject(workspace, handle)
      .then((p) => {
        if (!cancelled)
          setTarget(p.board_readable === false ? { key, to: null, project: p, error: null } : { key, to: p.board_url, error: null })
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
  if (fresh?.project) return <WorkspaceProjectReadOnly project={fresh.project} />
  if (fresh?.error) return <p className="p-6 text-sm text-destructive">{fresh.error}</p>
  return <p className="p-6 text-sm text-muted-foreground">Opening {key}…</p>
}

/** A workspace project for a reader outside its steward's workspace: what it is and its tasks. */
function WorkspaceProjectReadOnly({ project }: { project: ProjectDetailOut }): JSX.Element {
  const tasks = project.tasks ?? []
  return (
    <div className="p-6 max-w-3xl">
      <h1 className="text-lg font-semibold text-foreground">{project.name}</h1>
      <WorkspaceProjectLine project={project} />
      <ProjectKbLink project={project} />
      <ProjectMcpInstall project={project} />
      {project.outcome && <p className="text-[13px] text-foreground-secondary mt-3">{project.outcome}</p>}
      {project.drive_folder_url && (
        <p className="text-[12px] mt-2">
          <a href={project.drive_folder_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            Drive folder
          </a>
        </p>
      )}
      <h2 className="text-sm font-semibold text-foreground mt-6 mb-2">
        Tasks · {project.open_task_count} open of {project.task_count}
      </h2>
      {tasks.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">No tasks yet.</p>
      ) : (
        <ul className="divide-y divide-border border border-border rounded-lg">
          {tasks.map((t) => (
            <li key={`${t.agent_slug}/${t.ext_id}`} className="px-3 py-2 text-[13px] flex gap-3">
              <span className="flex-1 min-w-0 truncate text-foreground">{t.title}</span>
              <span className="text-muted-foreground font-mono text-[11px]">{t.agent_slug}</span>
              <span className="text-muted-foreground text-[11px]">{t.status}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
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

/** A link to a knowledge-base project's sources and its owner's review (`…/projects/:handle/kb`). */
export function ProjectKbLink({ project }: { project: ProjectOut }): JSX.Element | null {
  if (project.scope !== 'workspace' || project.mcp_kind !== 'kb') return null
  // The address names the project's own workspace, which for a division's project
  // kept from the parent is not the board's.
  const [ws, handle] = project.address.split('/')
  if (!ws || !handle) return null
  return (
    <p className="text-[12px] mt-2" data-testid="project-kb-link">
      <Link to={scopedPath(ws, `/projects/${encodeURIComponent(handle)}/kb`)} className="text-primary hover:underline">
        Knowledge base sources &amp; review →
      </Link>
    </p>
  )
}

/** How to add a project's own MCP server: one URL, nothing to download. Only for a
 *  project that serves something (`mcp_path`). The name is the handle, so two
 *  projects added side by side never collide. */
export function ProjectMcpInstall({ project }: { project: ProjectOut }): JSX.Element | null {
  if (!project.mcp_path) return null
  const url = `${window.location.origin}${project.mcp_path}`.replace(/\/$/, '')
  return (
    <section className="mt-4 rounded-lg border border-border bg-card p-4 max-w-3xl" data-testid="project-mcp-install">
      <h2 className="text-sm font-semibold text-foreground">Use its tools</h2>
      <p className="text-[12px] text-muted-foreground mt-0.5">
        Its own MCP server: only this project&apos;s tools, answered by canopy, with nothing to download. You sign in
        with your canopy account the first time.
      </p>
      <p className="text-[12px] text-foreground mt-3">Claude Code</p>
      <CopyCommand command={`claude mcp add --transport http ${project.handle} ${url}`} testId="project-mcp-claude-code" />
      <p className="text-[12px] text-foreground mt-3">Claude (web or desktop): Settings → Connectors → Add custom connector</p>
      <CopyCommand command={url} testId="project-mcp-url" />
    </section>
  )
}
