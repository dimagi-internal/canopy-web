import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { useAuth } from '@/auth/AuthProvider'
import { createWorkspace } from '@/api/workspaces'
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

  if (state === 'loading') return null
  if (state === 'ready' && !alwaysOfferForm) return null
  // On /new-workspace an eligible user sees the form even though they already
  // belong somewhere; `needs-invite` still applies, because eligibility is the
  // server's call either way.
  const offerForm = shouldOfferCreateForm({ state, canCreate, alwaysOfferForm })

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

  return (
    <div className="mx-auto max-w-xl px-6 py-16">
      <h1 className="text-lg font-semibold text-foreground">Welcome to Canopy</h1>
      <p className="mt-3 text-[13px] leading-relaxed text-foreground-secondary">
        Canopy runs a fleet of AI agents and keeps the record of what they do. Everything
        that belongs to a team — projects, agents, chats, demos — lives inside a{' '}
        <span className="font-medium text-foreground">workspace</span>. You are not in one yet.
      </p>

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
          <p className="text-[12px] text-muted-foreground">
            Already invited to one? Open the /invite/… link a colleague sent you instead.
          </p>
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
