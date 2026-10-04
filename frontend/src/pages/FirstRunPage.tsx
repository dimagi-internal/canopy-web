import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { useAuth } from '@/auth/AuthProvider'
import {
  createWorkspace,
  listRequestableWorkspaces,
  requestWorkspaceAccess,
  type RequestableWorkspaceOut,
} from '@/api/workspaces'
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
  const [requestable, setRequestable] = useState<RequestableWorkspaceOut[]>([])
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [sendingSlug, setSendingSlug] = useState<string | null>(null)
  // Slugs whose request is waiting on an admin — from the server's
  // `pending_request_id`, or set when a request we just sent came back pending.
  const [pendingSlugs, setPendingSlugs] = useState<Set<string>>(new Set())
  const [requestError, setRequestError] = useState('')

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

  // Requesting an invitation is a THIRD option alongside create/needs-invite,
  // not a replacement for either. Fetched here (not lazily) because a stranded
  // user with no workspace is exactly who this list is for; it is itself a
  // capability list (it never names a workspace the caller may not ask to
  // join), which is what makes rendering it unconditionally safe. Fetched in
  // the `ready` state too, so someone already in one workspace can ask into
  // another from /new-workspace. Nothing here joins anyone: an admin approves
  // (or, where the workspace auto-approves, the request comes back approved).
  useEffect(() => {
    if (state === 'loading') return
    let cancelled = false
    listRequestableWorkspaces()
      .then((rows) => {
        if (cancelled) return
        setRequestable(rows)
        setPendingSlugs(new Set(rows.filter((r) => r.pending_request_id != null).map((r) => r.slug)))
      })
      .catch(() => {
        // Best-effort: the request section just stays empty on failure — the
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

  async function handleRequest(ws: RequestableWorkspaceOut) {
    setSendingSlug(ws.slug)
    setRequestError('')
    try {
      const req = await requestWorkspaceAccess(ws.slug, (notes[ws.slug] ?? '').trim())
      if (req.status === 'approved') {
        // Auto-approved: they are in. WorkspaceProvider's membership list never
        // invalidates itself, so refresh before the redirect resolves.
        await refresh()
        navigate(`/w/${ws.slug}`)
        return
      }
      setPendingSlugs((prev) => new Set(prev).add(ws.slug))
    } catch (e) {
      setRequestError(e instanceof Error ? e.message : 'Could not send the request.')
    } finally {
      setSendingSlug(null)
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
            Workspaces keep separate teams&apos; projects, agents and demos apart.{' '}
            {requestable.length > 0
              ? 'Create another below, or ask to be invited into one your address can request.'
              : 'Create another below.'}
          </>
        ) : (
          <>
            Canopy runs a fleet of AI agents and keeps the record of what they do. Everything
            that belongs to a team — projects, agents, chats, demos — lives inside a{' '}
            <span className="font-medium text-foreground">workspace</span>. You are not in one yet.
          </>
        )}
      </p>

      {requestable.length > 0 ? (
        <div className="mt-8 space-y-3">
          <h2 className="text-sm font-semibold text-foreground">
            {alreadyAMember ? 'Or request an invitation' : 'Request an invitation'}
          </h2>
          {requestError ? <p className="text-[13px] text-destructive">{requestError}</p> : null}
          <ul className="space-y-2">
            {requestable.map((ws) => {
              const pending = pendingSlugs.has(ws.slug)
              return (
                <li key={ws.slug} className="rounded-lg border border-border bg-card p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-[13px] font-medium text-foreground">{ws.display_name}</p>
                      <p className="text-[12px] text-muted-foreground">
                        Your {ws.domain} address can request an invitation.
                      </p>
                    </div>
                    {pending ? (
                      <span className="shrink-0 rounded bg-muted px-2 py-1 text-[12px] text-foreground-secondary">
                        Requested
                      </span>
                    ) : null}
                  </div>
                  {pending ? (
                    <p className="mt-2 text-[12px] text-foreground-secondary">
                      Requested — an admin will review it. You&apos;ll get an email when they decide.
                    </p>
                  ) : (
                    <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-center">
                      <input
                        aria-label={`Note to the admins of ${ws.display_name} (optional)`}
                        value={notes[ws.slug] ?? ''}
                        onChange={(e) => setNotes((prev) => ({ ...prev, [ws.slug]: e.target.value }))}
                        maxLength={1000}
                        placeholder="Optional note to the admins — who you are, what you need"
                        className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
                      />
                      <button
                        type="button"
                        disabled={sendingSlug === ws.slug}
                        onClick={() => void handleRequest(ws)}
                        className="shrink-0 rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                      >
                        {sendingSlug === ws.slug ? 'Sending…' : `Request an invitation to ${ws.display_name}`}
                      </button>
                    </div>
                  )}
                </li>
              )
            })}
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
            Your account can join a workspace but cannot create one. Ask a workspace admin
            or owner to invite you — they can do it from their workspace&apos;s Members page — and
            open the /invite/… link they send you.
          </p>
        </div>
      )}
    </div>
  )
}
