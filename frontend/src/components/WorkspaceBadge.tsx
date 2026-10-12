import { useParams } from 'react-router-dom'
import { useActiveWorkspace } from '@/workspace/WorkspaceProvider'

// A list pinned to a workspace also carries the rows of the workspaces below it
// that you belong to (`/w/dimagi/agents` shows connect's agents too — the read
// rule, docs/architecture/roles.md). This marks the rows that live somewhere
// other than the page's workspace, so a connect agent on the dimagi page says so.
// Renders nothing for a row in the page's own workspace, or when either is unknown.
export function WorkspaceBadge({ workspace, className = '' }: {
  workspace?: string | null
  className?: string
}) {
  const { workspace: routeWorkspace } = useParams()
  const active = useActiveWorkspace()
  const here = routeWorkspace || active
  if (!workspace || !here || workspace === here) return null
  return (
    <span
      data-testid="workspace-badge"
      title={`In the ${workspace} workspace`}
      className={`inline-flex shrink-0 items-center rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-medium leading-none text-muted-foreground ${className}`}
    >
      {workspace}
    </span>
  )
}
