// Workspace (tenant) context. The active workspace is driven by the URL's
// :workspace segment (source of truth); this provider fetches the caller's
// memberships so the header switcher can render and so a bare/legacy route can
// redirect to a sensible default.
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { listWorkspaces, type WorkspaceOut } from '../api/workspaces'
import { resolveActiveWorkspace } from './resolveActiveWorkspace'

interface WorkspaceCtx {
  workspaces: WorkspaceOut[]
  active: string | null
  loading: boolean
  // Re-fetch `workspaces` on demand. The list (incl. each membership's
  // `role`) is fetched once on mount and otherwise never invalidated — a
  // page that mutates the CALLER's own role in a workspace (e.g. an owner
  // demoting themselves on WorkspaceMembersPage) must call this afterward,
  // or every consumer of this context (header switcher, per-page
  // isOwner/role checks) keeps rendering the stale pre-mutation role until a
  // full reload, then gets surprised by a 403 on the very next owner-only
  // action it still thinks it can take.
  refresh: () => Promise<void>
}

const Ctx = createContext<WorkspaceCtx | null>(null)

export function WorkspaceProvider({
  urlSlug,
  children,
}: {
  urlSlug: string | null
  children: ReactNode
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceOut[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const ws = await listWorkspaces()
    setWorkspaces(ws)
  }, [])

  useEffect(() => {
    let live = true
    listWorkspaces()
      .then((ws) => {
        if (live) setWorkspaces(ws)
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [])

  const active = resolveActiveWorkspace(workspaces, urlSlug)
  return <Ctx.Provider value={{ workspaces, active, loading, refresh }}>{children}</Ctx.Provider>
}

export function useWorkspace(): WorkspaceCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useWorkspace must be used within WorkspaceProvider')
  return ctx
}
