import { useEffect, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import {
  addWorkspaceVaultMember,
  attachWorkspaceVault,
  deleteWorkspaceVaultSecret,
  detachWorkspaceVault,
  getWorkspaceVaultZk,
  removeWorkspaceVaultMember,
  type WorkspaceVaultOut,
  type ZkTargetOut,
} from '@/api/vault'
import { Button } from 'canopy-ui/ui'
import { Section } from '@/components/objectPage/settingsLayout'
import { runnerHref } from '@/lib/scopedLinks'
import { VAULT_ROLE_LABEL } from '@/pages/agents/AgentVaultMembersSection'
import {
  grantWorkspaceVaultPending, makeWorkspaceVaultKey, rotateWorkspaceVaultKey, sealWorkspaceVaultValue,
} from '@/vault/operations'
import { memberWaiting, VaultWaitingNote } from '@/vault/VaultWaiting'
import type { VaultOutletContext } from './VaultPage'

// A workspace vault's tabs (VaultPage.tsx). Who may change what is the server's rule
// for each route; a control is offered only to someone who manages the vault.

const inputClass =
  'min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-56'
const buttonClass =
  'min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0'
const quietClass =
  'min-h-11 rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40 sm:min-h-0'

export function managesVault(v: WorkspaceVaultOut): boolean {
  return v.you === 'owner' || v.you === 'admin'
}

/** Why someone is a member, in words. */
export function sourceLabel(source: string): string {
  if (source === 'direct') return 'Added by name'
  const [kind, name] = source.split(':')
  if (kind === 'workspace_owners') return `Owner of ${name}`
  if (kind === 'workspace_admins') return `Admin of ${name}`
  if (kind === 'attached') return `Full access to ${name}`
  if (kind === 'agent') return `Full access to ${name}`
  if (kind === 'license') return `Licensed to use ${name}`
  return source
}

/** One write at a time, its error shown: the tabs' shared busy/error state. */
function useRun(onChange: (v: WorkspaceVaultOut) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (fn: () => Promise<WorkspaceVaultOut | void>) => {
    setBusy(true)
    setError(null)
    try {
      const next = await fn()
      if (next) onChange(next)
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save')
      return false
    } finally {
      setBusy(false)
    }
  }
  const errorLine = error && <p className="mt-2 text-[12px] text-destructive" data-testid="vault-error">{error}</p>
  return { busy, run, errorLine }
}

/** Its key (made in this browser, copies to whoever waits) and what it serves. */
export function VaultOverviewTab({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const [pending, setPending] = useState<readonly ZkTargetOut[]>([])
  const [target, setTarget] = useState('')

  useEffect(() => {
    let live = true
    if (manages && v.kv > 0) {
      getWorkspaceVaultZk(v.vault_id)
        .then((z) => { if (live) setPending(z.pending) })
        .catch(() => { if (live) setPending([]) })
    }
    return () => { live = false }
  }, [manages, v.vault_id, v.kv])
  // People waiting are granted from the key line; each runner from its own row below.
  const peopleWaiting = pending.filter((p) => p.kind !== 'device')
  const runnerWaiting = (runnerId: string) => pending.some((p) => p.kind === 'device' && p.runner === runnerId)
  const grant = (only: (t: ZkTargetOut) => boolean) => run(async () => {
    await grantWorkspaceVaultPending(v.vault_id, await unlock(), only)
    setPending((ps) => ps.filter((p) => !only(p)))
    reload()
  })

  return (
    <div>
      <Section id="key" title="Its key">
        {v.kv === 0 ? (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 p-2 text-[12px]"
            data-testid="vault-no-key">
            <span className="text-foreground">
              No key yet, so nothing can go in. Your passkey makes it: a copy for you, and one for every member and
              machine waiting.
            </span>
            {manages && (
              <Button type="button" size="sm" disabled={busy} data-testid="vault-make-key"
                onClick={() => void run(async () => { await makeWorkspaceVaultKey(v.vault_id, await unlock()); reload() })}>
                Make its key
              </Button>
            )}
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2 text-[12px]" data-testid="vault-key">
            <span className="text-success">●</span>
            <span className="text-foreground">Key version {v.kv}</span>
            {peopleWaiting.length > 0 && (
              <Button type="button" size="sm" variant="outline" disabled={busy} data-testid="vault-grant"
                onClick={() => void grant((p) => p.kind !== 'device')}>
                Give {peopleWaiting.length === 1 ? 'the person waiting' : `the ${peopleWaiting.length} people waiting`} a copy
              </Button>
            )}
            {manages && (
              <Button type="button" size="sm" variant="outline" disabled={busy} data-testid="vault-rotate"
                title="A fresh key: everyone and every machine that still belongs gets a copy, and a copy kept by anyone removed opens nothing written after"
                onClick={() => void run(async () => { await rotateWorkspaceVaultKey(v.vault_id, await unlock()); reload() })}>
                Change the key
              </Button>
            )}
          </div>
        )}
        <VaultWaitingNote members={v.members} holders={v.holders} testId={`ws-vault-waiting-${v.vault_id}`} />
      </Section>

      <Section id="serves" title="Access">
        {v.attachments.length === 0 && <p className="text-[12px] text-muted-foreground">Serves nothing yet.</p>}
        {v.attachments.length > 0 && (
          <table className="w-full text-[12px]" data-testid="ws-vault-serves">
            <tbody className="divide-y divide-border">
              {v.attachments.map((a) => (
                <tr key={a.id}>
                  <td className="py-1.5 text-foreground">
                    {a.runner
                      ? <Link to={runnerHref({ id: a.runner, workspace: v.workspace })} className="text-primary hover:underline">
                          runner {a.runner_name ?? a.runner}
                        </Link>
                      : a.agent ? `agent ${a.agent}` : `every agent in ${a.workspace} and below`}
                  </td>
                  <td className="py-1.5 text-muted-foreground">
                    {a.runner ? 'its Claude credentials' : 'layered under each agent’s own secrets'}
                  </td>
                  <td className="py-1.5" data-testid={`ws-vault-access-${a.id}`}>
                    {a.runner && v.kv > 0 && (
                      v.holders.keepers.includes(a.runner_name ?? '')
                        ? <span className="text-success">can open it</span>
                        : runnerWaiting(a.runner)
                          ? (
                            <span className="flex flex-wrap items-center gap-2">
                              <span className="text-warning">waiting for a copy</span>
                              {manages && (
                                <button type="button" disabled={busy} data-testid={`ws-vault-grant-${a.id}`}
                                  className={buttonClass}
                                  onClick={() => void grant((p) => p.kind === 'device' && p.runner === a.runner)}>
                                  Give it a copy
                                </button>
                              )}
                            </span>
                          )
                          : <span className="text-muted-foreground">no copy (no approved key on it yet)</span>
                    )}
                  </td>
                  <td className="py-1.5 text-right">
                    {manages && (
                      <button type="button" disabled={busy} data-testid={`ws-vault-detach-${a.id}`} className={quietClass}
                        onClick={() => void run(() => detachWorkspaceVault(v.vault_id, a.id))}>
                        Stop serving
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {manages && (
          <form className="mt-3 flex flex-wrap items-center gap-2" data-testid="ws-vault-attach"
            onSubmit={(e) => {
              e.preventDefault()
              const t = target.trim()
              // "workspace:<slug>" serves a workspace and below; anything else is an agent.
              const to = t.startsWith('workspace:') ? { workspace: t.slice(10) } : { agent: t }
              void run(() => attachWorkspaceVault(v.vault_id, to)).then((ok) => ok && setTarget(''))
            }}>
            <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="agent slug, or workspace:slug"
              aria-label={`Serve an agent or workspace from ${v.name}`} className={inputClass} />
            <button type="submit" disabled={busy || !target.trim()} className={buttonClass}>Serve it</button>
          </form>
        )}
      </Section>
      {errorLine}
    </div>
  )
}

/** Who can open it, and why each person is one. */
export function VaultMembersTab({ vault: v, onChange }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const [email, setEmail] = useState('')
  return (
    <Section id="members" title="Members">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
            <th className="py-1 font-normal">Person</th><th className="py-1 font-normal">Role</th>
            <th className="py-1 font-normal">Why</th><th />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {v.members.map((m) => (
            <tr key={m.user_id} data-testid={`ws-vault-member-${m.user_id}`}>
              <td className="py-1.5 text-foreground">{m.email}</td>
              <td className="py-1.5 text-muted-foreground">{VAULT_ROLE_LABEL[m.role] ?? m.role}</td>
              <td className="py-1.5 text-muted-foreground">
                {m.sources.map(sourceLabel).join(' · ')}
                {memberWaiting(m, v.holders) && <span className="text-warning"> · {memberWaiting(m, v.holders)}</span>}
              </td>
              <td className="py-1.5 text-right">
                {manages && m.sources.includes('direct') && (
                  <button type="button" disabled={busy} data-testid={`ws-vault-remove-${m.user_id}`} className={quietClass}
                    onClick={() => void run(() => removeWorkspaceVaultMember(v.vault_id, m.user_id))}>
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {manages && (
        <form className="mt-3 flex flex-wrap items-center gap-2" data-testid="ws-vault-add-member"
          onSubmit={(e) => {
            e.preventDefault()
            void run(() => addWorkspaceVaultMember(v.vault_id, email.trim(), 'member')).then((ok) => ok && setEmail(''))
          }}>
          <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" placeholder="name@dimagi.com"
            aria-label={`Add a person to ${v.name}`} className={inputClass} />
          <button type="submit" disabled={busy || !email.trim()} className={buttonClass}>Add person</button>
        </form>
      )}
      {errorLine}
    </Section>
  )
}

/** The names it holds (never a value); add or replace one, sealed in this browser. */
export function VaultSecretsTab({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  return (
    <Section id="secrets" title="Secrets">
      {v.secrets.length === 0 && <p className="text-[12px] text-muted-foreground">None yet.</p>}
      {v.secrets.length > 0 && (
        <table className="w-full text-[12px]" data-testid="ws-vault-secrets">
          <tbody className="divide-y divide-border">
            {v.secrets.map((s) => (
              <tr key={s}>
                <td className="py-1.5 font-mono text-foreground">{s}</td>
                <td className="py-1.5 text-right">
                  {manages && (
                    <button type="button" disabled={busy} className={quietClass} data-testid={`ws-vault-delete-${s}`}
                      onClick={() => window.confirm(`Delete ${s} from ${v.name}?`)
                        && void run(() => deleteWorkspaceVaultSecret(v.vault_id, s))}>
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {manages && v.kv > 0 && (
        <form className="mt-3 flex flex-wrap items-center gap-2" data-testid="ws-vault-seal"
          onSubmit={(e) => {
            e.preventDefault()
            void run(async () => {
              await sealWorkspaceVaultValue(v.vault_id, name.trim(), value, await unlock())
              reload()
            }).then((ok) => { if (ok) { setName(''); setValue('') } })
          }}>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="NAME" aria-label="Secret name"
            className={inputClass} />
          <input value={value} onChange={(e) => setValue(e.target.value)} type="password" autoComplete="off"
            placeholder="value" aria-label="Secret value" className={inputClass} />
          <button type="submit" disabled={busy || !name.trim() || !value} className={buttonClass}>Seal it in</button>
        </form>
      )}
      {manages && v.kv === 0 && (
        <p className="mt-2 text-[12px] text-muted-foreground">Make the vault&rsquo;s key (Overview) before anything can go in.</p>
      )}
      <p className="mt-2 text-[11px] text-muted-foreground">
        A value is encrypted in this browser under the vault&rsquo;s key; canopy-web stores only ciphertext and shows only names.
      </p>
      {errorLine}
    </Section>
  )
}
