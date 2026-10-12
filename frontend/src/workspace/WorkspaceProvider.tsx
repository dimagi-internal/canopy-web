// Workspace (tenant) context. The active workspace is driven by the URL's
// :workspace segment (source of truth); this provider fetches the caller's
// memberships so the header switcher can render and so a bare/legacy route can
// redirect to a sensible default. Off a tenant route, the workspace the caller
// last had in the URL stays active (remembered per browser), so leaving a
// tenant page for /supervisor or /sessions keeps the workspace they picked.
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

// Per-browser convenience, so storage failing (private mode, blocked site data)
// only costs the memory, never the page.
const REMEMBERED_KEY = 'canopy:lastWorkspace'

function readRemembered(): string | null {
  try {
    return localStorage.getItem(REMEMBERED_KEY)
  } catch {
    return null
  }
}

function writeRemembered(slug: string): void {
  try {
    localStorage.setItem(REMEMBERED_KEY, slug)
  } catch {
    /* storage unavailable — the URL still drives tenant pages */
  }
}

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

  const active = resolveActiveWorkspace(workspaces, urlSlug, readRemembered())
  // Remember a workspace only once it is confirmed a membership — a bad slug in
  // a pasted URL must not become the default for every root page after it.
  useEffect(() => {
    if (urlSlug && workspaces.some((w) => w.slug === urlSlug)) writeRemembered(urlSlug)
  }, [urlSlug, workspaces])
  return <Ctx.Provider value={{ workspaces, active, loading, refresh }}>{children}</Ctx.Provider>
}

/** The caller's role in `slug`, or null when it is unknown — outside a
 *  provider, still loading, or not a member. Display hints only. */
export function useWorkspaceRole(slug: string | null | undefined): string | null {
  const ctx = useContext(Ctx)
  if (!ctx || !slug) return null
  return ctx.workspaces.find((w) => w.slug === slug)?.role ?? null
}

/** The active workspace (the URL's on a tenant page), or null outside a
 *  provider. Display hints only — never a gate. */
export function useActiveWorkspace(): string | null {
  return useContext(Ctx)?.active ?? null
}

/** The caller's workspaces, or [] outside a provider or while loading. */
export function useWorkspaceList(): WorkspaceOut[] {
  return useContext(Ctx)?.workspaces ?? []
}

export function useWorkspace(): WorkspaceCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useWorkspace must be used within WorkspaceProvider')
  return ctx
}
