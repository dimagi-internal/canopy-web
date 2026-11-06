import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { getWorkspaceVault, type WorkspaceVaultOut } from '@/api/vault'
import { ObjectPage } from '@/components/objectPage/ObjectPage'
import { VaultView } from '@/components/vault/VaultView'
import { scopedPath } from '@/lib/scopedLinks'
import type { Unlocked } from '@/vault/operations'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import {
  machineCount, servedOf, VaultKeyTab, VaultMachinesTab, VaultMakeKey, VaultPeopleTab, VaultSecretsTab, VaultServedTab,
} from './VaultTabs'

// A WORKSPACE VAULT'S ONE PAGE, `/w/:workspace/vaults/:vaultId`, drawn by the same
// `VaultView` an agent's vault uses (components/vault/VaultView.tsx): a summary
// line, then Secrets, Access (People, Agents, Projects, Workspaces, Machines) and
// Encryption key as collapsible sections
// (docs/architecture/pages.md, "One editor per object"). Settings → Secrets lists a
// workspace's vaults and links here; a runner's credentials link to the vault that
// holds them. This file is the workspace vault's ADAPTER: it reads the vault and
// gives the frame each section's body and one-line summary.
//
// The old tab addresses (`/overview`, `/members`, `/secrets`) still work: each opens
// the section that tab became (`/overview` → Encryption key, `/members` → Access).

export interface VaultOutletContext {
  vault: WorkspaceVaultOut
  /** A write changed it server-side: the page shows the fresh row. */
  onChange: (vault: WorkspaceVaultOut) => void
  /** Read it again (after a key-state write, which answers in another shape). */
  reload: () => void
  /** My vault key, unlocked, for a step that needs it. */
  unlock: () => Promise<Unlocked>
}

export function VaultPage(): JSX.Element {
  const { workspace = '', vaultId = '', tab } = useParams()
  const [vault, setVault] = useState<WorkspaceVaultOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const unlocker = useVaultUnlock()

  const load = useCallback(() => {
    setError(null)
    getWorkspaceVault(vaultId)
      .then(setVault)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load this vault'))
  }, [vaultId])
  useEffect(() => {
    setVault(null)
    load()
  }, [vaultId, load])

  const back = (
    <Link to={scopedPath(workspace, '/settings/secrets')} className="text-primary hover:underline">All vaults →</Link>
  )
  if (error) {
    return (
      <div className="max-w-4xl">
        <p className="text-[12px]">{back}</p>
        <p className="mt-6 text-sm text-destructive" data-testid="vault-page-error">{error}</p>
        <button type="button" onClick={load} className="mt-2 text-[12px] text-primary hover:underline">Try again</button>
      </div>
    )
  }
  if (!vault) return <div className="h-8 w-64 animate-pulse rounded-md bg-muted" data-testid="vault-page-loading" />
  if (vault.workspace !== workspace) return <Navigate to={scopedPath(vault.workspace, `/vaults/${vault.vault_id}`)} replace />

  const ctx: VaultOutletContext = { vault, onChange: setVault, reload: load, unlock: unlocker.unlock }
  const machines = machineCount(vault)
  const waitingRunners = vault.attachments.filter((a) => a.runner && a.access === 'waiting').length
  const waiting = vault.members.filter((m) => m.has_vault_key && !m.has_copy).length
  const noKey = vault.kv === 0
  // A vault with no key says so once, as its first step above the sections.
  const attention = [
    !noKey && waiting > 0 && `${waiting} ${waiting === 1 ? 'person is' : 'people are'} waiting for the key`,
  ].filter((a): a is string => Boolean(a))
  const agents = servedOf(vault, 'agents').length
  const projects = servedOf(vault, 'projects').length
  const workspaces = servedOf(vault, 'workspaces').length
  const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
  const accessSummary = [
    count(vault.members.length, 'person', 'people'),
    agents && count(agents, 'agent'),
    projects && count(projects, 'project'),
    workspaces && count(workspaces, 'workspace'),
    count(machines, 'machine'),
  ].filter(Boolean).join(' · ')

  return (
    <ObjectPage
      title={vault.name}
      context="Vault"
      subtitle={`A workspace vault in ${vault.workspace}: only its members, and the machines they work for, can open it — canopy-web never can.`}
      actions={back}
    >
      <VaultView
        facts={{
          secrets: vault.secrets.length,
          people: vault.members.length,
          machines,
          attention,
        }}
        firstStep={noKey ? <VaultMakeKey {...ctx} /> : undefined}
        secrets={{
          summary: vault.secrets.length ? vault.secrets.join(', ') : 'none yet',
          tone: 'ok',
          defaultOpen: !tab || tab === 'secrets',
          body: <VaultSecretsTab {...ctx} />,
        }}
        access={{
          summary: accessSummary,
          tone: waiting > 0 || waitingRunners > 0 ? 'attention' : 'quiet',
          defaultOpen: tab === 'members',
          anchors: ['members', 'serves'],
          intro: <>Who gets this vault&rsquo;s secrets. An agent, project or workspace given the vault reads them in its
            turns; the people who can use it become members, and the machines that run it hold a copy of the key.</>,
          tabs: [
            { kind: 'people', count: vault.members.length, attention: waiting > 0, body: <VaultPeopleTab {...ctx} /> },
            { kind: 'agents', count: agents, body: <VaultServedTab {...ctx} kind="agents" /> },
            { kind: 'projects', count: projects, body: <VaultServedTab {...ctx} kind="projects" /> },
            { kind: 'workspaces', count: workspaces, body: <VaultServedTab {...ctx} kind="workspaces" /> },
            { kind: 'machines', count: machines, attention: waitingRunners > 0, anchors: ['runners'],
              body: <VaultMachinesTab {...ctx} /> },
          ],
        }}
        encryptionKey={{
          summary: noKey ? 'no key yet' : `version ${vault.kv}`,
          tone: noKey ? 'attention' : 'quiet',
          defaultOpen: tab === 'overview',
          anchors: ['key'],
          body: <VaultKeyTab {...ctx} />,
        }}
      />
      {unlocker.element}
    </ObjectPage>
  )
}
