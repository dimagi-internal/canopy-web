import { useEffect, useState } from 'react'
import {
  addVaultMember,
  getVaultMembers,
  removeVaultMember,
  setVaultAgentLink,
  type VaultMembersOut,
} from '@/api/vault'

// Who may open this agent's vault (spec 2026-10-22-vault-access-follows-canopy-
// access-design.md). Membership is the vault's own: people added by name, plus
// a link that makes everyone with full access to the agent a member. Each row
// says WHY the person is a member, so the page always answers "why can they?".

const ROLE_LABEL: Record<string, string> = { manage: 'Manages', use: 'Can use' }

function sourceLabel(source: string, slug: string): string {
  if (source === 'direct') return 'Added by name'
  const [kind, name] = source.split(':')
  if (kind === 'agent') return name === slug ? 'Full access to this agent' : `Full access to ${name}`
  if (kind === 'license') return name === slug ? 'Licensed to use this agent' : `Licensed to use ${name}`
  if (kind === 'workspace_owners') return `Owner of ${name}`
  if (kind === 'workspace_admins') return `Admin of ${name}`
  return source
}

export function AgentVaultMembersSection({ slug }: { slug: string }) {
  const [state, setState] = useState<VaultMembersOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'use' | 'manage'>('use')

  useEffect(() => {
    let cancelled = false
    getVaultMembers(slug)
      .then((s) => !cancelled && setState(s))
      .catch(() => !cancelled && setState(null))
    return () => {
      cancelled = true
    }
  }, [slug])

  if (state === null) return null
  const manages = state.you === 'manage'

  const run = async (fn: () => Promise<VaultMembersOut>) => {
    setBusy(true)
    setError(null)
    try {
      setState(await fn())
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
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Who is in this vault
      </h3>
      <p className="mb-2 text-[12px] text-muted-foreground" data-testid="vault-link-line">
        {state.agent_link.linked
          ? 'Everyone with full access to this agent is a member, so giving someone the agent gives them its vault.'
          : 'This vault keeps its own list: giving someone the agent does not give them its vault.'}
      </p>
      <table className="w-full text-[12px]" data-testid="vault-members-table">
        <tbody className="divide-y divide-border">
          {state.members.map((m) => (
            <tr key={m.user_id} data-testid={`vault-member-${m.user_id}`}>
              <td className="py-1.5 text-foreground">{m.email}</td>
              <td className="py-1.5 text-muted-foreground">{ROLE_LABEL[m.role] ?? m.role}</td>
              <td className="py-1.5 text-muted-foreground">
                {m.sources.map((s) => sourceLabel(s, slug)).join(' · ')}
                {!m.has_vault_key && <span className="text-warning"> · no vault key yet</span>}
              </td>
              <td className="py-1.5 text-right">
                {manages && m.sources.includes('direct') && (
                  <button type="button" disabled={busy} data-testid={`vault-remove-${m.user_id}`}
                          onClick={() => void run(() => removeVaultMember(slug, m.user_id))}
                          className="min-h-11 rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40 sm:min-h-0">
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {manages && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <form className="flex flex-wrap items-center gap-2" data-testid="vault-add-form"
                onSubmit={(e) => {
                  e.preventDefault()
                  void run(() => addVaultMember(slug, email.trim(), role)).then((ok) => ok && setEmail(''))
                }}>
            <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" placeholder="name@dimagi.com"
                   aria-label="Add a person by email"
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-64" />
            <select value={role} onChange={(e) => setRole(e.target.value as 'use' | 'manage')} aria-label="Role"
                    className="min-h-11 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0">
              <option value="use">Can use</option>
              <option value="manage">Manages</option>
            </select>
            <button type="submit" disabled={busy || !email.trim()}
                    className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
              Add
            </button>
          </form>
          <button type="button" disabled={busy} data-testid="vault-toggle-link"
                  onClick={() => void run(() => setVaultAgentLink(slug, !state.agent_link.linked))}
                  className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
            {state.agent_link.linked ? 'Stop following this agent’s access' : 'Follow this agent’s access'}
          </button>
        </div>
      )}
      {error && <p className="mt-2 text-[12px] text-destructive" data-testid="vault-members-error">{error}</p>}
    </section>
  )
}
