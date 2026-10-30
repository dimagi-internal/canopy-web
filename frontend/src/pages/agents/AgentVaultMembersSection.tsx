import { useEffect, useState } from 'react'
import {
  addVaultMember,
  getVaultMembers,
  removeVaultMember,
  setVaultAgentLink,
  type VaultMembersOut,
} from '@/api/vault'
import { VaultMembers } from '@/components/vault/VaultMembers'

// Who may open this agent's vault (spec 2026-10-22-vault-access-follows-canopy-
// access-design.md). Membership is the vault's own: people added by name, plus
// a link that makes everyone with full access to the agent a member. Each row
// says WHY the person is a member, so the page always answers "why can they?".
// The list itself is the shared `VaultMembers`; this is the agent's data and writes.

export function AgentVaultMembersSection({
  slug,
  onLoaded,
}: {
  slug: string
  /** Told the members once they are read (and after each change), for the vault's summary. */
  onLoaded?: (state: VaultMembersOut | null) => void
}) {
  const [state, setState] = useState<VaultMembersOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    getVaultMembers(slug)
      .then((s) => {
        if (cancelled) return
        setState(s)
        onLoaded?.(s)
      })
      .catch(() => {
        if (cancelled) return
        setState(null)
        onLoaded?.(null)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  if (state === null) return null
  const manages = state.you === 'owner' || state.you === 'admin'

  const run = async (fn: () => Promise<VaultMembersOut>) => {
    setBusy(true)
    setError(null)
    try {
      const next = await fn()
      setState(next)
      onLoaded?.(next)
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save')
      return false
    } finally {
      setBusy(false)
    }
  }

  return (
    <section id="cred-vault-members" className="mb-6" data-testid="vault-members">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">People</h4>
      <VaultMembers
        label="this vault"
        members={state.members}
        holders={state.holders}
        manages={manages}
        busy={busy}
        error={error}
        self={slug}
        pickRole
        ids={{
          table: 'vault-members-table',
          row: (id) => `vault-member-${id}`,
          remove: (id) => `vault-remove-${id}`,
          form: 'vault-add-form',
          waiting: 'vault-waiting',
          error: 'vault-members-error',
        }}
        note={
          <span data-testid="vault-link-line">
            {state.agent_link.linked
              ? 'Everyone with full access to this agent is a member, so giving someone the agent gives them its vault.'
              : 'This vault keeps its own list: giving someone the agent does not give them its vault.'}
          </span>
        }
        onAdd={(email, role) => run(() => addVaultMember(slug, email, role))}
        onRemove={(userId) => void run(() => removeVaultMember(slug, userId))}
        extra={
          <button type="button" disabled={busy} data-testid="vault-toggle-link"
                  onClick={() => void run(() => setVaultAgentLink(slug, !state.agent_link.linked))}
                  className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
            {state.agent_link.linked ? 'Stop including everyone with access to the agent' : 'Include everyone with access to the agent'}
          </button>
        }
      />
    </section>
  )
}
