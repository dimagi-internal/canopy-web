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
import { VaultMembers } from '@/components/vault/VaultMembers'
import { storePlace } from '@/components/settings/machineText'
import { runnerHref } from '@/lib/scopedLinks'
import {
  grantWorkspaceVaultPending, makeWorkspaceVaultKey, rotateWorkspaceVaultKey, sealWorkspaceVaultValue,
} from '@/vault/operations'
import type { VaultOutletContext } from './VaultPage'
import { Td, Th, Tr } from '@/components/DataTable'

// A workspace vault's three bodies — Storage & key, Who can open them, Secrets —
// drawn by VaultPage inside the shared VaultView as its sections (the old tab addresses still open them). Who may change what is the server's rule
// for each route; a control is offered only to someone who manages the vault.

const inputClass =
  'min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0 sm:w-56'
const buttonClass =
  'min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0'
const quietClass =
  'min-h-11 rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40 sm:min-h-0'

/** A titled block inside one of the vault's sections. */
function Sub({ id, title, children }: { id: string; title?: string; children: React.ReactNode }): JSX.Element {
  return (
    <div id={id} className="mb-5 scroll-mt-6">
      {title && <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{title}</h4>}
      {children}
    </div>
  )
}

export function managesVault(v: WorkspaceVaultOut): boolean {
  return v.you === 'owner' || v.you === 'admin'
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
  // People waiting are granted from the key line; each runner from its row under Who can open them.
  const peopleWaiting = pending.filter((p) => p.kind !== 'device')
  // Machines that reach this vault through an agent, workspace or project it serves have
  // no runner row below: list them here, each with its own grant (Shared, 2026-11-05).
  const attachedRunners = new Set(v.attachments.filter((a) => a.runner).map((a) => a.runner))
  const machinesWaiting = pending.filter((p) => p.kind === 'device' && !attachedRunners.has(p.runner))
  const served = v.attachments.filter((a) => !a.runner)
  const grant = (only: (t: ZkTargetOut) => boolean) => run(async () => {
    await grantWorkspaceVaultPending(v.vault_id, await unlock(), only)
    setPending((ps) => ps.filter((p) => !only(p)))
    reload()
  })

  return (
    <div>
      <Sub id="key" title="Its key">
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
                Give {peopleWaiting.length === 1 ? 'the person waiting' : `the ${peopleWaiting.length} people waiting`} access
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
        {manages && v.kv > 0 && machinesWaiting.length > 0 && (
          <div className="mt-3" data-testid="vault-machines-waiting">
            <p className="mb-1 text-[12px] text-foreground">
              {machinesWaiting.length === 1 ? '1 machine is' : `${machinesWaiting.length} machines are`} waiting for
              this vault&rsquo;s key
            </p>
            <ul className="divide-y divide-border text-[12px]">
              {machinesWaiting.map((p) => (
                <li key={p.target} data-testid={`vault-machine-waiting-${p.target}`}
                  className="flex flex-col gap-1 py-1.5 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <span className="font-medium text-foreground">{p.runner_name || 'A machine'}</span>
                    <span className="text-muted-foreground">
                      {p.person_email ? ` — ${p.person_email}'s` : ''}
                      {p.serves?.length ? ` · runs ${p.serves.join(', ')}` : ''}
                    </span>
                    {p.waiting_reason === 'unendorsed' && (
                      <p className="text-warning" data-testid={`vault-machine-unendorsed-${p.target}`}>
                        Not confirmed by {p.person_email || 'its person'}, so no keeper will pass it the key. Your
                        grant is the way in.
                      </p>
                    )}
                  </div>
                  <button type="button" disabled={busy} className={buttonClass}
                    data-testid={`vault-machine-grant-${p.target}`}
                    title="Your passkey gives this machine a copy of this vault's key"
                    onClick={() => void grant((t) => t.kind === 'device' && t.target === p.target)}>
                    Give access
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Sub>

      <Sub id="serves" title="Access">
        <p className="mb-2 text-[12px] text-muted-foreground">
          The agents whose secrets this vault adds to. The runners that read it are under Who can open them.
        </p>
        {served.length === 0 && <p className="text-[12px] text-muted-foreground">No agent reads it yet.</p>}
        {served.length > 0 && (
          <table className="w-full text-[12px]" data-testid="ws-vault-serves">
            <tbody className="divide-y divide-border">
              {served.map((a) => (
                <tr key={a.id}>
                  <Td density="cozy" tone="foreground">
                    {a.project
                      ? `member agents of project ${a.project}`
                      : a.agent ? `agent ${a.agent}` : `every agent in ${a.workspace} and below`}
                  </Td>
                  <Td density="cozy" tone="muted" data-testid={a.project ? `ws-vault-project-${a.id}` : undefined}>
                    {a.project ? projectServing(a) : 'layered under each agent’s own secrets'}
                  </Td>
                  <Td density="cozy" align="right">
                    {manages && (
                      <button type="button" disabled={busy} data-testid={`ws-vault-detach-${a.id}`} className={quietClass}
                        onClick={() => void run(() => detachWorkspaceVault(v.vault_id, a.id))}>
                        Stop serving
                      </button>
                    )}
                  </Td>
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
              // "workspace:<slug>" serves a workspace and below, "project:<workspace>/<handle>" a
              // project's member agents; anything else is an agent.
              const to = t.startsWith('workspace:') ? { workspace: t.slice(10) }
                : t.startsWith('project:') ? { project: t.slice(8) } : { agent: t }
              void run(() => attachWorkspaceVault(v.vault_id, to)).then((ok) => ok && setTarget(''))
            }}>
            <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="agent slug, workspace:slug or project:workspace/handle"
              aria-label={`Serve an agent, workspace or project from ${v.name}`} className={inputClass} />
            <button type="submit" disabled={busy || !target.trim()} className={buttonClass}>Serve it</button>
          </form>
        )}
      </Sub>
      {errorLine}
    </div>
  )
}

/** What a project attachment serves right now: its member agents in the vault's tree, and those it cannot. */
function projectServing(a: WorkspaceVaultOut['attachments'][number]): string {
  const served = a.served ?? []
  const notServed = a.not_served ?? []
  const first = served.length ? `serves ${served.join(', ')}` : 'serves no agent now'
  const rest = a.project_outside_tree
    ? `; the project itself is outside this vault's workspace tree, so none of ${notServed.join(', ') || 'its agents'} is served`
    : notServed.length ? `; not served (outside the vault's tree): ${notServed.join(', ')}` : ''
  return first + rest
}

/** Who can open it: the people, and why each is one; the runners, and where each one's key lives. */
export function VaultMembersTab({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const runners = v.attachments.filter((a) => a.runner)
  return (
    <>
    <Sub id="members" title="People">
      <VaultMembers
        label={v.name}
        members={v.members}
        holders={v.holders}
        manages={manages}
        busy={busy}
        self={undefined}
        ids={{
          row: (id) => `ws-vault-member-${id}`,
          remove: (id) => `ws-vault-remove-${id}`,
          form: 'ws-vault-add-member',
          waiting: `ws-vault-waiting-${v.vault_id}`,
        }}
        onAdd={(email) => run(() => addWorkspaceVaultMember(v.vault_id, email, 'member'))}
        onRemove={(userId) => void run(() => removeWorkspaceVaultMember(v.vault_id, userId))}
      />
      {errorLine}
    </Sub>
    {runners.length > 0 && (
      <Sub id="runners" title="Machines (runners)">
        <p className="mb-2 text-[12px] text-muted-foreground">
          Each reads its Claude credentials from this vault with its own copy of the vault&rsquo;s key, locked to a
          key that never leaves its hardware.
        </p>
        <table className="w-full text-[12px]" data-testid="ws-vault-runners">
          <thead>
            <Tr head="label">
              <Th weight="normal">Runner</Th><Th weight="normal">Its key</Th>
              <Th weight="normal">Access</Th><Th density="none" weight="inherit"><span className="sr-only">Actions</span></Th>
            </Tr>
          </thead>
          <tbody className="divide-y divide-border">
            {runners.map((a) => (
              <tr key={a.id} data-testid={`ws-vault-runner-${a.id}`}>
                <Td density="cozy">
                  <Link to={runnerHref({ id: a.runner!, workspace: v.workspace })} className="text-primary hover:underline">
                    {a.runner_name ?? a.runner}
                  </Link>
                </Td>
                <Td density="cozy" tone="muted">{a.key_place ? storePlace(a.key_place) : 'no key yet'}</Td>
                <Td density="cozy" data-testid={`ws-vault-access-${a.id}`}>
                  {a.access === 'has' && <span className="text-success">has access</span>}
                  {a.access === 'waiting' && (
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-warning">waiting for access</span>
                      {manages && (
                        <button type="button" disabled={busy} data-testid={`ws-vault-grant-${a.id}`} className={buttonClass}
                          title="Your passkey gives this runner access to what is stored in this vault"
                          onClick={() => void run(async () => {
                            await grantWorkspaceVaultPending(v.vault_id, await unlock(),
                              (p) => p.kind === 'device' && p.runner === a.runner)
                            reload()
                          })}>
                          Give access
                        </button>
                      )}
                    </span>
                  )}
                  {a.access === 'none' && <span className="text-muted-foreground">no access yet (no approved key on it)</span>}
                </Td>
                <Td density="cozy" align="right">
                  {manages && (
                    <button type="button" disabled={busy} data-testid={`ws-vault-detach-${a.id}`} className={quietClass}
                      onClick={() => void run(() => detachWorkspaceVault(v.vault_id, a.id))}>
                      Remove
                    </button>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </Sub>
    )}
    </>
  )
}

/** The names it holds (never a value); add or replace one, sealed in this browser. */
export function VaultSecretsTab({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  return (
    <Sub id="vault-secrets">
      {v.secrets.length === 0 && <p className="text-[12px] text-muted-foreground">None yet.</p>}
      {v.secrets.length > 0 && (
        <table className="w-full text-[12px]" data-testid="ws-vault-secrets">
          <tbody className="divide-y divide-border">
            {v.secrets.map((s) => (
              <tr key={s}>
                <Td density="cozy" tone="foreground" mono>{s}</Td>
                <Td density="cozy" align="right">
                  {manages && (
                    <button type="button" disabled={busy} className={quietClass} data-testid={`ws-vault-delete-${s}`}
                      onClick={() => window.confirm(`Delete ${s} from ${v.name}?`)
                        && void run(() => deleteWorkspaceVaultSecret(v.vault_id, s))}>
                      Delete
                    </button>
                  )}
                </Td>
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
        <p className="mt-2 text-[12px] text-muted-foreground">Make the vault&rsquo;s key (Storage &amp; key) before anything can go in.</p>
      )}
      <p className="mt-2 text-[11px] text-muted-foreground">
        A value is encrypted in this browser under the vault&rsquo;s key; canopy-web stores only ciphertext and shows only names.
      </p>
      {errorLine}
    </Sub>
  )
}
