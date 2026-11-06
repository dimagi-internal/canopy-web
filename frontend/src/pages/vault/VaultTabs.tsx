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
import { listWorkspaces, type WorkspaceOut } from '@/api/workspaces'
import { useDirectory } from '@/components/work/useDirectory'
import { ProjectPicker, WorkspacePicker } from '@/components/work/bits'
import { AgentPicker } from '@/components/work/projectBits'
import { Td, Th, Tr } from '@/components/DataTable'

// A workspace vault's bodies — Secrets, each Access tab (People, Agents, Projects, Workspaces,
// Machines) and Encryption key — drawn by VaultPage inside the shared VaultView. Who may change what is the server's rule
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

/** A new vault's first step, shown above its sections: make its key, in this browser.
 *  Says who gets a copy now and who only once they have set up their own vault key. */
export function VaultMakeKey({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const later = v.members.filter((m) => !m.has_vault_key).map((m) => m.email)
  return (
    <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-[12px]" data-testid="vault-no-key">
      <p className="font-medium text-foreground">First, make this vault&rsquo;s key</p>
      <p className="mt-1 text-foreground">
        Nothing can go in until it has one.{' '}
        {managesVault(v)
          ? 'Your passkey makes it, with a copy for you and for every member and machine waiting.'
          : 'An owner or admin of this vault makes it.'}
      </p>
      {later.length > 0 && (
        <p className="mt-1 text-muted-foreground" data-testid="vault-no-key-later">
          {later.join(', ')} {later.length === 1 ? 'has' : 'have'} not set up a vault key yet, so{' '}
          {later.length === 1 ? 'gets a copy' : 'get copies'} once they do.
        </p>
      )}
      {managesVault(v) && (
        <Button type="button" size="sm" className="mt-2" disabled={busy} data-testid="vault-make-key"
          onClick={() => void run(async () => { await makeWorkspaceVaultKey(v.vault_id, await unlock()); reload() })}>
          Make its key
        </Button>
      )}
      {errorLine}
    </div>
  )
}

/** Encryption key: its version, giving people who wait a copy, and changing it. */
export function VaultKeyTab({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const [pending, setPending] = useState<readonly ZkTargetOut[]>([])

  useEffect(() => {
    let live = true
    if (manages && v.kv > 0) {
      getWorkspaceVaultZk(v.vault_id)
        .then((z) => { if (live) setPending(z.pending) })
        .catch(() => { if (live) setPending([]) })
    }
    return () => { live = false }
  }, [manages, v.vault_id, v.kv])
  // People waiting are granted here; each runner from its row on the Machines tab.
  const peopleWaiting = pending.filter((p) => p.kind !== 'device')
  const grant = (only: (t: ZkTargetOut) => boolean) => run(async () => {
    await grantWorkspaceVaultPending(v.vault_id, await unlock(), only)
    setPending((ps) => ps.filter((p) => !only(p)))
    reload()
  })

  return (
    <Sub id="key">
      {v.kv === 0 ? (
        <p className="text-[12px] text-muted-foreground" data-testid="vault-no-key-here">
          No key yet: make it at the top of this page.
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-2 text-[12px]" data-testid="vault-key">
          <span className="text-success">●</span>
          <span className="text-foreground">Key version {v.kv}</span>
          <span className="text-muted-foreground">
            · held by {plural(v.members.filter((m) => m.has_copy).length, 'person', 'people')}
            {v.holders.keepers.length > 0 && ` and ${plural(v.holders.keepers.length, 'machine')}`}
          </span>
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
      {errorLine}
    </Sub>
  )
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/** What a project attachment serves right now: its member agents in the vault's tree, and those it cannot. */
function projectServing(a: Attachment): string {
  const served = a.served ?? []
  const notServed = a.not_served ?? []
  const first = served.length ? `serves ${served.join(', ')}` : 'serves no agent now'
  const rest = a.project_outside_tree
    ? `; the project itself is outside this vault's workspace tree, so none of ${notServed.join(', ') || 'its agents'} is served`
    : notServed.length ? `; not served (outside the vault's tree): ${notServed.join(', ')}` : ''
  return first + rest
}

/** People tab: who may open it, and why each one may. */
export function VaultPeopleTab({ vault: v, onChange }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  return (
    <Sub id="members">
      <VaultMembers
        label={v.name}
        members={v.members}
        holders={v.holders}
        manages={managesVault(v)}
        busy={busy}
        self={undefined}
        ids={{
          row: (id) => `ws-vault-member-${id}`,
          remove: (id) => `ws-vault-remove-${id}`,
          form: 'ws-vault-add-member',
          waiting: `ws-vault-waiting-${v.vault_id}`,
        }}
        note={<>Someone is a member when added here by name, or because they can use an agent this vault serves.</>}
        onAdd={(email) => run(() => addWorkspaceVaultMember(v.vault_id, email, 'member'))}
        onRemove={(userId) => void run(() => removeWorkspaceVaultMember(v.vault_id, userId))}
      />
      {errorLine}
    </Sub>
  )
}

/** Machines tab: runners whose own Claude credentials live here, and the machines holding a
 *  copy of the key that pass it on to whoever is waiting. */
export function VaultMachinesTab({ vault: v, onChange, reload, unlock }: VaultOutletContext): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const runners = v.attachments.filter((a) => a.runner)
  return (
    <Sub id="runners">
      {v.holders.keepers.length > 0 && (
        <p className="mb-3 text-[12px] text-foreground" data-testid="ws-vault-keepers">
          Holding a copy of the key: {v.holders.keepers.join(', ')}.{' '}
          <span className="text-muted-foreground">
            Each runs an agent this vault serves for one of its owners or admins, and passes the key on to people and
            machines waiting for it.
          </span>
        </p>
      )}
      {runners.length === 0 && v.holders.keepers.length === 0 && (
        <p className="text-[12px] text-muted-foreground">No machine holds a copy yet.</p>
      )}
      {runners.length > 0 && (
        <>
        <p className="mb-2 text-[12px] text-muted-foreground">
          Runners whose own Claude credentials live here. Each reads them with its own copy of the key, locked to a
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
        </>
      )}
      {errorLine}
    </Sub>
  )
}

/** The machines that can open it: runners attached, and keepers. One name counted once. */
export function machineCount(v: WorkspaceVaultOut): number {
  return new Set([...v.attachments.filter((a) => a.runner).map((a) => a.runner_name ?? a.runner), ...v.holders.keepers]).size
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
        <p className="mt-2 text-[12px] text-muted-foreground">Make the vault&rsquo;s key (at the top of this page) before anything can go in.</p>
      )}
      <p className="mt-2 text-[11px] text-muted-foreground">
        A value is encrypted in this browser under the vault&rsquo;s key; canopy-web stores only ciphertext and shows only names.
      </p>
      {errorLine}
    </Sub>
  )
}

type Attachment = WorkspaceVaultOut['attachments'][number]
type ServedKind = 'agents' | 'projects' | 'workspaces'

/** The vault's workspace and every workspace below it that the viewer can see: the only places
 *  an attachment can reach. `parent` links each workspace to the one above it. */
function useVaultTree(root: string): WorkspaceOut[] {
  const [all, setAll] = useState<WorkspaceOut[]>([])
  useEffect(() => {
    let live = true
    listWorkspaces().then((ws) => { if (live) setAll(ws) }).catch(() => { if (live) setAll([]) })
    return () => { live = false }
  }, [])
  const bySlug = new Map(all.map((w) => [w.slug, w]))
  const inTree = (w: WorkspaceOut): boolean => {
    const seen = new Set<string>()
    for (let cur: WorkspaceOut | undefined = w; cur && !seen.has(cur.slug); cur = cur.parent ? bySlug.get(cur.parent) : undefined) {
      if (cur.slug === root) return true
      seen.add(cur.slug)
    }
    return false
  }
  return all.filter(inTree)
}

/** The attachments of one kind (never runners: those are machines). */
export function servedOf(v: WorkspaceVaultOut, kind: ServedKind): Attachment[] {
  const served = v.attachments.filter((a) => !a.runner)
  if (kind === 'agents') return served.filter((a) => a.agent)
  if (kind === 'projects') return served.filter((a) => a.project)
  return served.filter((a) => !a.agent && !a.project && a.workspace)
}

const SERVED_TEXT: Record<ServedKind, { empty: string; add: string; pickLabel: (vault: string) => string }> = {
  agents: { empty: 'No agent gets this vault on its own.', add: 'Add an agent…', pickLabel: (n) => `Give an agent ${n}` },
  projects: { empty: 'No project gets this vault.', add: 'Add a project…', pickLabel: (n) => `Give a project ${n}` },
  workspaces: { empty: 'No whole workspace gets this vault.', add: 'Add a workspace…', pickLabel: (n) => `Give a whole workspace ${n}` },
}

/** One Access tab for agents, projects or whole workspaces: who gets the vault's secrets, and a
 *  picker of real ones to add (the work board's agent and project pickers, never a typed string).
 *  Adding says first exactly what it gives. */
export function VaultServedTab({ vault: v, onChange, kind }: VaultOutletContext & { kind: ServedKind }): JSX.Element {
  const { busy, run, errorLine } = useRun(onChange)
  const manages = managesVault(v)
  const directory = useDirectory(v.workspace)
  const tree = useVaultTree(v.workspace)
  const treeSlugs = new Set([v.workspace, ...tree.map((w) => w.slug)])
  const rows = servedOf(v, kind)
  const taken = new Set(rows.map((a) => a.agent ?? a.project ?? a.workspace))
  const agentName = (slug: string) => directory.agents.find((a) => a.slug === slug)?.name || slug
  const projectName = (address: string) => directory.projects.find((p) => p.address === address)?.name || address
  const workspaceName = (slug: string) => tree.find((w) => w.slug === slug)?.display_name || slug
  const name = (a: Attachment) =>
    kind === 'agents' ? agentName(a.agent!) : kind === 'projects' ? projectName(a.project!) : `${workspaceName(a.workspace!)} and below`
  const detail = (a: Attachment) =>
    kind === 'agents' ? 'layered under the agent’s own secrets'
      : kind === 'projects' ? projectServing(a) : 'every agent in it, layered under its own secrets'
  const secrets = v.secrets.length ? v.secrets.join(', ') : 'nothing yet'
  const attach = (to: { agent: string } | { project: string } | { workspace: string }, label: string) => {
    const ok = window.confirm(
      `Give ${label} ${v.name}?\n\nIt gets: ${secrets}.\nAnyone who can use it becomes a member of this vault.`)
    if (ok) void run(() => attachWorkspaceVault(v.vault_id, to))
  }
  const text = SERVED_TEXT[kind]
  const add = <span className={buttonClass + ' inline-block'}>{text.add}</span>

  return (
    <Sub id={`serves-${kind}`}>
      <div data-testid={`ws-vault-serves-${kind}`}>
        {rows.length === 0 ? <p className="text-[12px] text-muted-foreground">{text.empty}</p> : (
          <table className="w-full text-[12px]">
            <tbody className="divide-y divide-border">
              {rows.map((a) => (
                <tr key={a.id}>
                  <Td density="cozy" tone="foreground">{name(a)}</Td>
                  <Td density="cozy" tone="muted" data-testid={a.project ? `ws-vault-project-${a.id}` : undefined}>{detail(a)}</Td>
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
        )}
      </div>
      {manages && (
        <div className="mt-2" data-testid={`ws-vault-attach-${kind}`}>
          {kind === 'agents' && (
            <AgentPicker slug="" none={null} disabled={busy} label={text.pickLabel(v.name)} trigger={add}
              directory={{ agents: directory.agents.filter((a) => treeSlugs.has(a.workspace ?? '') && !taken.has(a.slug)) }}
              onChange={(slug) => attach({ agent: slug }, agentName(slug))} />
          )}
          {kind === 'projects' && (
            <ProjectPicker project="" none={null} disabled={busy} label={text.pickLabel(v.name)}
              directory={{ projects: directory.projects.filter((p) => treeSlugs.has(p.workspace) && !taken.has(p.address)) }}
              onChange={(address) => attach({ project: address }, projectName(address))}>
              {add}
            </ProjectPicker>
          )}
          {kind === 'workspaces' && (
            <WorkspacePicker disabled={busy} label={text.pickLabel(v.name)}
              workspaces={(tree.length ? tree : [{ slug: v.workspace, display_name: v.workspace }]).filter((w) => !taken.has(w.slug))}
              onChange={(slug) => attach({ workspace: slug }, `${workspaceName(slug)} and every workspace below it`)}>
              {add}
            </WorkspacePicker>
          )}
        </div>
      )}
      {errorLine}
    </Sub>
  )
}
