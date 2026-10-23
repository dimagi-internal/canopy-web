import { useEffect, useState, type JSX } from 'react'
import {
  addWorkspaceVaultMember,
  attachWorkspaceVault,
  createWorkspaceVault,
  detachWorkspaceVault,
  listWorkspaceVaults,
  removeWorkspaceVaultMember,
  type WorkspaceVaultOut,
} from '@/api/vault'
import { memberWaiting, VaultWaitingNote } from '@/vault/VaultWaiting'

// A workspace's own vaults (spec 2026-10-22-vault-access-follows-canopy-access-design.md
// §4): shared secrets its agents read, layered under each agent's own. Each vault has its
// own members — the workspace's owners manage it, and everyone with full access to an
// agent it serves can use it — and says, for every member, why they are one. Values are
// encrypted on a machine that holds the vault's key (`canopy vault zk set --vault`), so
// canopy-web never sees one.

function sourceLabel(source: string): string {
  if (source === 'direct') return 'Added by name'
  const [kind, name] = source.split(':')
  if (kind === 'workspace_owners') return `Owner of ${name}`
  if (kind === 'workspace_admins') return `Admin of ${name}`
  if (kind === 'attached') return `Full access to ${name}`
  if (kind === 'agent') return `Full access to ${name}`
  if (kind === 'license') return `Licensed to use ${name}`
  return source
}

function VaultCard({ v, onChange }: { v: WorkspaceVaultOut; onChange: (next: WorkspaceVaultOut) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [target, setTarget] = useState('')
  const manages = v.you === 'manage'

  const run = async (fn: () => Promise<WorkspaceVaultOut>) => {
    setBusy(true)
    setError(null)
    try {
      onChange(await fn())
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save')
      return false
    } finally {
      setBusy(false)
    }
  }

  return (
    <li id={`ws-vault-${v.vault_id}`} className="rounded-md border border-border p-3" data-testid={`ws-vault-${v.vault_id}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium text-foreground">{v.name}</span>
        <span className="text-[11px] text-muted-foreground">
          {v.kv ? `key version ${v.kv}` : 'no key yet'} · {v.secrets.length} secret{v.secrets.length === 1 ? '' : 's'}
        </span>
      </div>
      {v.secrets.length > 0 && (
        <p className="mt-1 text-[12px] text-muted-foreground" data-testid="ws-vault-secrets">{v.secrets.join(', ')}</p>
      )}
      <p className="mt-2 text-[12px] text-foreground" data-testid="ws-vault-serves">
        {v.attachments.length === 0
          ? 'Serves no agent yet.'
          : 'Serves: ' + v.attachments.map((a) => (a.agent ? a.agent : `every agent in ${a.workspace} and below`)).join('; ')}
      </p>
      {manages && v.attachments.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-2">
          {v.attachments.map((a) => (
            <button key={a.id} type="button" disabled={busy} data-testid={`ws-vault-detach-${a.id}`}
                    onClick={() => void run(() => detachWorkspaceVault(v.vault_id, a.id))}
                    className="min-h-11 rounded-md px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-40 sm:min-h-0">
              Stop serving {a.agent ?? a.workspace}
            </button>
          ))}
        </div>
      )}
      <table className="mt-2 w-full text-[12px]">
        <tbody className="divide-y divide-border">
          {v.members.map((m) => (
            <tr key={m.user_id} data-testid={`ws-vault-member-${m.user_id}`}>
              <td className="py-1 text-foreground">{m.email}</td>
              <td className="py-1 text-muted-foreground">{m.role === 'manage' ? 'Manages' : 'Can use'}</td>
              <td className="py-1 text-muted-foreground">
                {m.sources.map(sourceLabel).join(' · ')}
                {memberWaiting(m, v.holders) && <span className="text-warning"> · {memberWaiting(m, v.holders)}</span>}
              </td>
              <td className="py-1 text-right">
                {manages && m.sources.includes('direct') && (
                  <button type="button" disabled={busy} data-testid={`ws-vault-remove-${m.user_id}`}
                          onClick={() => void run(() => removeWorkspaceVaultMember(v.vault_id, m.user_id))}
                          className="min-h-11 rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40 sm:min-h-0">
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <VaultWaitingNote members={v.members} holders={v.holders} testId={`ws-vault-waiting-${v.vault_id}`} />
      {manages && (
        <div className="mt-2 flex flex-wrap gap-2">
          <form className="flex flex-wrap items-center gap-2" data-testid="ws-vault-add-member"
                onSubmit={(e) => {
                  e.preventDefault()
                  void run(() => addWorkspaceVaultMember(v.vault_id, email.trim(), 'use')).then((ok) => ok && setEmail(''))
                }}>
            <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" placeholder="name@dimagi.com"
                   aria-label={`Add a person to ${v.name}`}
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-56" />
            <button type="submit" disabled={busy || !email.trim()}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
              Add person
            </button>
          </form>
          <form className="flex flex-wrap items-center gap-2" data-testid="ws-vault-attach"
                onSubmit={(e) => {
                  e.preventDefault()
                  const t = target.trim()
                  // "workspace:<slug>" serves a whole workspace and below; anything else is an agent.
                  const to = t.startsWith('workspace:') ? { workspace: t.slice(10) } : { agent: t }
                  void run(() => attachWorkspaceVault(v.vault_id, to)).then((ok) => ok && setTarget(''))
                }}>
            <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="agent slug, or workspace:slug"
                   aria-label={`Serve an agent or workspace from ${v.name}`}
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-56" />
            <button type="submit" disabled={busy || !target.trim()}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
              Serve it
            </button>
          </form>
        </div>
      )}
      {manages && (
        <p className="mt-2 text-[11px] text-muted-foreground" data-testid="ws-vault-cli">
          {v.kv
            ? <>Add a secret from a machine that holds this vault&rsquo;s key: <code>canopy vault zk set --vault {v.vault_id} NAME</code></>
            : <>Make its key from one of your machines: <code>canopy vault zk enable --vault {v.vault_id}</code></>}
        </p>
      )}
      {error && <p className="mt-2 text-[12px] text-destructive">{error}</p>}
    </li>
  )
}

export function WorkspaceVaultsSection({ slug }: { slug: string }): JSX.Element | null {
  const [vaults, setVaults] = useState<WorkspaceVaultOut[] | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let off = false
    listWorkspaceVaults(slug).then((v) => !off && setVaults(v)).catch(() => !off && setVaults(null))
    return () => {
      off = true
    }
  }, [slug])

  if (vaults === null) return null
  const replace = (next: WorkspaceVaultOut) => setVaults((vs) => (vs ?? []).map((v) => (v.vault_id === next.vault_id ? next : v)))

  return (
    <section className="rounded-lg border border-border bg-card p-3" data-testid="workspace-vaults">
      <h2 className="mb-1 text-[13px] font-medium text-foreground">Workspace vaults</h2>
      <p className="mb-2 text-[12px] text-muted-foreground">
        Secrets this workspace shares with its agents, read under each agent&rsquo;s own (an agent&rsquo;s own secret wins).
        Only members can open one, and canopy-web never can. Giving someone full access to an agent a vault serves gives
        them that vault.
      </p>
      {vaults.length === 0 && <p className="text-[12px] text-muted-foreground">No vaults yet.</p>}
      <ul className="space-y-2">
        {vaults.map((v) => <VaultCard key={v.vault_id} v={v} onChange={replace} />)}
      </ul>
      <form className="mt-3 flex flex-wrap items-center gap-2" data-testid="ws-vault-create"
            onSubmit={(e) => {
              e.preventDefault()
              setError(null)
              createWorkspaceVault(slug, name.trim())
                .then((v) => {
                  setVaults((vs) => [...(vs ?? []), v])
                  setName('')
                })
                .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not make the vault'))
            }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Vault name" aria-label="New vault name"
               className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-56" />
        <button type="submit" disabled={!name.trim()}
                className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
          New vault
        </button>
      </form>
      {error && <p className="mt-2 text-[12px] text-destructive" data-testid="ws-vault-error">{error}</p>}
    </section>
  )
}
