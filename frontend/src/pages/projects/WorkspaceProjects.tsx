import type { JSX } from 'react'
import { Link } from 'react-router-dom'

import { CopyCommand } from '@/components/agents/CopyCommand'
import type { ProjectOut } from '@/api/agents'
import { scopedPath } from '@/lib/scopedLinks'

// What a project serves, on its page: the knowledge base's sources and review,
// and the one line that adds its MCP server. Every project is the workspace's,
// addressed `<workspace>/<handle>`.

/** The line a workspace project's page shows under its title. */
export function WorkspaceProjectLine({ project }: { project: ProjectOut }): JSX.Element | null {
  if (!project.handle) return null
  return (
    <p className="text-[12px] text-muted-foreground mt-1" data-testid="workspace-project-line">
      Workspace project <span className="font-mono">{project.address}</span> · kept current by{' '}
      {project.steward_slug} · any agent in the workspace can file tasks into it
    </p>
  )
}

/** A link to a knowledge-base project's sources and its owner's review (`…/projects/:handle/kb`). */
export function ProjectKbLink({ project }: { project: ProjectOut }): JSX.Element | null {
  if (project.mcp_kind !== 'kb') return null
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
