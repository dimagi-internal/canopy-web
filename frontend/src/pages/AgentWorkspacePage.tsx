import { useCallback, useEffect, useState } from 'react'
import { Link, Outlet, useParams } from 'react-router-dom'
import { getAgent, listTasks, type AgentDetailOut } from '@/api/agents'
import { AgentLeftNav } from '@/components/agents/AgentLeftNav'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { WorkbenchShell, WorkbenchMain } from 'canopy-ui'

/** Context the section sub-routes read via useOutletContext. */
export interface AgentOutletContext {
  agent: AgentDetailOut
  /** May the viewer dispatch / mark done — the server's `_agent_for_write`
   *  tier (`agent.work`, editor and above). The server decides; this only
   *  avoids offering a button that 403s. */
  canEdit: boolean
  /** Tasks waiting on the viewer (`?waiting=me`); `undefined` until known. The
   *  rail badge and the Tasks page's chip read this ONE number. */
  waiting: number | undefined
  /** Re-read `waiting` — call after anything that may move a task. */
  refreshWaiting: () => void
}

/**
 * The Agent Workspace shell: a full-bleed rail + scrolling main workbench. It
 * loads the agent detail once (counts for the rail badges + identity), then
 * renders the active section through <Outlet />. Each section lazy-loads its
 * own list data. Mirrors the DDD shell (DddShell + DddLeftNav).
 */
export function AgentWorkspacePage() {
  const { slug } = useParams<{ slug: string }>()
  const [agent, setAgent] = useState<AgentDetailOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const { workspaces } = useWorkspace()

  // ONE source for "waiting on you", stamped with the slug it counts so a
  // switch of agent never shows the previous agent's number.
  const [waitingFor, setWaitingFor] = useState<{ slug: string; n: number } | null>(null)
  const refreshWaiting = useCallback(() => {
    if (!slug) return
    listTasks(slug, { waiting: 'me' })
      .then((rows) => setWaitingFor({ slug, n: rows.length }))
      .catch(() => setWaitingFor(null))
  }, [slug])
  useEffect(() => refreshWaiting(), [refreshWaiting])
  const waiting = waitingFor && waitingFor.slug === slug ? waitingFor.n : undefined

  useEffect(() => {
    if (!slug) return
    let cancelled = false
    setLoading(true)
    setError(null)
    getAgent(slug)
      .then((detail) => {
        if (!cancelled) setAgent(detail)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load agent')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [slug])

  if (loading) {
    return (
      <WorkbenchShell>
        <aside className="w-64 shrink-0 border-r border-border bg-background/40 p-4">
          <div className="animate-pulse space-y-3">
            <div className="h-10 w-10 rounded-full bg-muted" />
            <div className="h-4 bg-muted rounded w-2/3" />
            <div className="h-3 bg-muted/70 rounded w-1/2" />
          </div>
        </aside>
        <WorkbenchMain className="px-6 py-8">
          <div className="max-w-4xl animate-pulse space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="bg-card border border-border rounded-xl p-5">
                <div className="h-4 bg-muted rounded w-2/3 mb-2" />
                <div className="h-3 bg-muted/70 rounded w-full" />
              </div>
            ))}
          </div>
        </WorkbenchMain>
      </WorkbenchShell>
    )
  }

  if (error || !agent) {
    return (
      <div className="px-6 py-8 max-w-4xl">
        <Link to="/agents" className="text-[12px] text-muted-foreground hover:text-primary transition-colors">
          ← Agents
        </Link>
        <div className="flex items-center justify-center h-48 text-destructive text-sm">
          {error ?? 'Agent not found'}
        </div>
      </div>
    )
  }

  const canEdit = roleAllows(workspaces.find((w) => w.slug === agent.workspace)?.role, 'agent.work')

  return (
    <WorkbenchShell>
      <AgentLeftNav agent={agent} waiting={waiting} />
      <WorkbenchMain>
        <Outlet context={{ agent, canEdit, waiting, refreshWaiting } satisfies AgentOutletContext} />
      </WorkbenchMain>
    </WorkbenchShell>
  )
}
