import { useState, type ReactNode } from 'react'

import { memberWaiting, VaultWaitingNote } from '@/vault/VaultWaiting'

// The people who may open a vault, and WHY each one may — one list for an agent's
// vault and a workspace's. The page that owns the vault supplies the data and the
// writes; this draws them the same way everywhere.

/** The vault's roles, without contacts: owners and admins change who is in it. */
export const VAULT_ROLE_LABEL: Record<string, string> = { owner: 'Owner', admin: 'Admin', member: 'Member' }

/** Why someone is a member, in words. `self` is the agent whose page this is, if any. */
export function sourceLabel(source: string, self?: string): string {
  if (source === 'direct') return 'Added by name'
  const [kind, name] = source.split(':')
  if (kind === 'workspace_owners') return `Owner of ${name}`
  if (kind === 'workspace_admins') return `Admin of ${name}`
  if (kind === 'agent' || kind === 'attached') return name === self ? 'Full access to this agent' : `Full access to ${name}`
  if (kind === 'license') return name === self ? 'Licensed to use this agent' : `Licensed to use ${name}`
  return source
}

export interface VaultPerson {
  user_id: number
  email: string
  role: string
  sources: readonly string[]
  has_vault_key: boolean
  has_copy?: boolean
}

export interface VaultMembersIds {
  table?: string
  row: (id: number) => string
  remove: (id: number) => string
  form: string
  waiting: string
  error?: string
}

const inputClass =
  'min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-64'

export function VaultMembers({
  label,
  members,
  holders,
  manages,
  busy,
  error,
  self,
  pickRole = false,
  ids,
  onAdd,
  onRemove,
  note,
  extra,
}: {
  /** Names the person is added to, for the add field's accessible name. */
  label: string
  members: readonly VaultPerson[]
  holders?: { has_key?: boolean; people?: readonly string[]; keepers?: readonly string[] }
  manages: boolean
  busy: boolean
  error?: string | null
  self?: string
  /** Offer Member/Admin when adding (an agent's vault); otherwise everyone added is a member. */
  pickRole?: boolean
  ids: VaultMembersIds
  onAdd: (email: string, role: 'member' | 'admin') => Promise<boolean>
  onRemove: (userId: number) => void
  /** A line above the table. */
  note?: ReactNode
  /** Controls beside the add form. */
  extra?: ReactNode
}) {
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'member' | 'admin'>('member')
  return (
    <div>
      {note && <p className="mb-2 text-[12px] text-muted-foreground">{note}</p>}
      <table className="w-full text-[12px]" data-testid={ids.table}>
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
            <th className="py-1 font-normal">Person</th>
            <th className="py-1 font-normal">Role</th>
            <th className="py-1 font-normal">Why they can</th>
            <th />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {members.map((m) => (
            <tr key={m.user_id} data-testid={ids.row(m.user_id)}>
              <td className="py-1.5 text-foreground">{m.email}</td>
              <td className="py-1.5 text-muted-foreground">{VAULT_ROLE_LABEL[m.role] ?? m.role}</td>
              <td className="py-1.5 text-muted-foreground">
                {m.sources.map((s) => sourceLabel(s, self)).join(' · ')}
                {memberWaiting(m, holders) && <span className="text-warning"> · {memberWaiting(m, holders)}</span>}
              </td>
              <td className="py-1.5 text-right">
                {manages && m.sources.includes('direct') && (
                  <button
                    type="button"
                    disabled={busy}
                    data-testid={ids.remove(m.user_id)}
                    onClick={() => onRemove(m.user_id)}
                    className="min-h-11 rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40 sm:min-h-0"
                  >
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <VaultWaitingNote members={members} holders={holders} testId={ids.waiting} />
      {manages && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <form
            className="flex flex-wrap items-center gap-2"
            data-testid={ids.form}
            onSubmit={(e) => {
              e.preventDefault()
              void onAdd(email.trim(), role).then((ok) => ok && setEmail(''))
            }}
          >
            <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" placeholder="name@dimagi.com"
              aria-label={`Add a person to ${label}`} className={inputClass} />
            {pickRole && (
              <select value={role} onChange={(e) => setRole(e.target.value as 'member' | 'admin')} aria-label="Role"
                className="min-h-11 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0">
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            )}
            <button type="submit" disabled={busy || !email.trim()}
              className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
              Add
            </button>
          </form>
          {extra}
        </div>
      )}
      {error && <p className="mt-2 text-[12px] text-destructive" data-testid={ids.error}>{error}</p>}
    </div>
  )
}
