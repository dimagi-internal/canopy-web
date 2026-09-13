import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { useAuth } from '@/auth/AuthProvider'
import { createWorkspace, joinWorkspace, listJoinableWorkspaces, type JoinableWorkspaceOut } from '@/api/workspaces'
import { firstRunState, shouldOfferCreateForm } from './firstRun'

/**
 * What a brand-new user sees. Replaces the two `return null` sites in
 * router.tsx, which rendered a blank page for anyone with no workspace
 * membership — the first screen of the power-user rollout.
 *
 * Doubles as documentation: this is the only page every new user is
 * guaranteed to read, so it explains what a workspace IS rather than just
 * asking for a slug.
 */
export function FirstRunPage({ alwaysOfferForm = false }: { alwaysOfferForm?: boolean }) {
  const { workspaces, loading, refresh } = useWorkspace()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [slug, setSlug] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [joinable, setJoinable] = useState<JoinableWorkspaceOut[]>([])
  const [joiningSlug, setJoiningSlug] = useState<string | null>(null)
  const [joinError, setJoinError] = useState('')

  // AuthProvider resolves `useAuth()` before any route mounts (it gates on
  // `status === 'loading'` itself), so `user` is always the real MeOut here —
  // no second fetch, no null-while-pending state to represent. `?? false`
  // stays fail-closed: defaulting an unresolved eligibility to "can create"
  // would offer a form that 403s (the F1 gate this screen exists to respect).
  const canCreate = user?.can_create_workspace ?? false

  const state = firstRunState({
    loading,
    workspaceCount: workspaces.length,
    canCreate,
  })

  // Joining is a THIRD option alongside create/needs-invite, not a
  // replacement for either — offer it only in the zero-workspace states.
  // Fetched here (not lazily on demand) because a stranded user with no
  // workspace is exactly the audience this list exists for; `listJoinableWorkspaces`
  // is itself a capability list (never over-discloses), so rendering it
  // unconditionally in this state is safe.
  useEffect(() => {
    if (state === 'loading' || state === 'ready') return
    let cancelled = false
    listJoinableWorkspaces()
      .then((rows) => {
        if (!cancelled) setJoinable(rows)
      })
      .catch(() => {
        // Best-effort: the join section just stays empty on failure — the
        // create/needs-invite path below is still fully usable.
      })
    return () => {
      cancelled = true
    }
  }, [state])

  if (state === 'loading') return null
  if (state === 'ready' && !alwaysOfferForm) return null
  // On /new-workspace an eligible user sees the form even though they already
  // belong somewhere; `needs-invite` still applies, because eligibility is the
  // server's call either way.
  const offerForm = shouldOfferCreateForm({ state, canCreate, alwaysOfferForm })

  async function handleJoin(ws: JoinableWorkspaceOut) {
    setJoiningSlug(ws.slug)
    setJoinError('')
    try {
      const joined = await joinWorkspace(ws.slug)
      // Same reason as submit() below: WorkspaceProvider's membership list
      // never invalidates itself, so the brand-new membership needs an
      // explicit refresh before the redirect lands somewhere that resolves.
      await refresh()
      navigate(`/w/${joined.slug}`)
    } catch (e) {
      setJoinError(e instanceof Error ? e.message : 'Could not join the workspace.')
      setJoiningSlug(null)
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    const res = await createWorkspace(slug.trim(), displayName.trim() || slug.trim())
    setBusy(false)
    if ('error' in res) {
      setError(res.error)
      return
    }
    // WorkspaceProvider fetches the membership list once on mount and never
    // invalidates it, so a brand-new membership is invisible until something
    // re-fetches. `refresh()` is the provider's documented mechanism for exactly
    // this ("a page that mutates the CALLER's own role in a workspace must call
    // this afterward") — use it rather than a full page reload.
    await refresh()
    navigate(`/w/${res.slug}`)
  }

  // `state === 'ready'` means the caller already belongs to a workspace —
  // the primary way to reach this page IS `/new-workspace` now that it and
  // the header link exist, so the copy must not tell an existing member they
  // have no workspace (that was the C2 regression).
  const alreadyAMember = state === 'ready'

  return (
    <div className="mx-auto max-w-xl px-6 py-16">
      <h1 className="text-lg font-semibold text-foreground">
        {alreadyAMember ? 'New workspace' : 'Welcome to Canopy'}
      </h1>
      <p className="mt-3 text-[13px] leading-relaxed text-foreground-secondary">
        {alreadyAMember ? (
          <>
            Workspaces keep separate teams&apos; projects, agents and demos apart.
            Create another below.
          </>
        ) : (
          <>
            Canopy runs a fleet of AI agents and keeps the record of what they do. Everything
            that belongs to a team — projects, agents, chats, demos — lives inside a{' '}
            <span className="font-medium text-foreground">workspace</span>. You are not in one yet.
          </>
        )}
      </p>

      {!alreadyAMember && joinable.length > 0 ? (
        <div className="mt-8 space-y-3">
          <h2 className="text-sm font-semibold text-foreground">Join a workspace</h2>
          {joinError ? <p className="text-[13px] text-destructive">{joinError}</p> : null}
          <ul className="space-y-2">
            {joinable.map((ws) => (
              <li
                key={ws.slug}
                className="flex items-center justify-between rounded-lg border border-border bg-card p-3"
              >
                <div>
                  <p className="text-[13px] font-medium text-foreground">{ws.display_name}</p>
                  <p className="text-[12px] text-muted-foreground">
                    Your {ws.domain} address is allowed to join.
                  </p>
                </div>
                <button
                  type="button"
                  disabled={joiningSlug === ws.slug}
                  onClick={() => handleJoin(ws)}
                  className="shrink-0 rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {joiningSlug === ws.slug ? 'Joining…' : 'Join'}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {offerForm ? (
        <form onSubmit={submit} className="mt-8 space-y-4">
          <div>
            <label htmlFor="ws-slug" className="block text-xs font-medium text-foreground-secondary">
              Workspace address
            </label>
            <input
              id="ws-slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="acme"
              required
              className="mt-1 w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
            />
            <p className="mt-1 text-[11px] text-muted-foreground">
              Used in every URL: /w/&lt;address&gt;. Lowercase letters, numbers and dashes.
            </p>
          </div>
          <div>
            <label htmlFor="ws-name" className="block text-xs font-medium text-foreground-secondary">
              Display name <span className="text-muted-foreground">(optional)</span>
            </label>
            <input
              id="ws-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Acme"
              className="mt-1 w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
            />
          </div>
          {error ? <p className="text-[13px] text-destructive">{error}</p> : null}
          <button
            type="submit"
            disabled={busy || !slug.trim()}
            className="rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {busy ? 'Creating…' : 'Create workspace'}
          </button>
          {!alreadyAMember && (
            <p className="text-[12px] text-muted-foreground">
              Already invited to one? Open the /invite/… link a colleague sent you instead.
            </p>
          )}
        </form>
      ) : (
        <div className="mt-8 rounded-lg border border-border bg-card p-4">
          <h2 className="text-sm font-semibold text-foreground">You need an invite</h2>
          <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
            Your account can join a workspace but cannot create one. Ask a workspace owner
            to invite you — they can do it from their workspace&apos;s Members page — and open
            the /invite/… link they send you.
          </p>
        </div>
      )}
    </div>
  )
}
