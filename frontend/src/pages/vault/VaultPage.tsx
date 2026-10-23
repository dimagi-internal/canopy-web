import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, Navigate, Outlet, useOutletContext, useParams } from 'react-router-dom'
import { getWorkspaceVault, type WorkspaceVaultOut } from '@/api/vault'
import { ObjectPage } from '@/components/objectPage/ObjectPage'
import type { ObjectTab } from '@/components/objectPage/ObjectTabs'
import { scopedPath } from '@/lib/scopedLinks'
import type { Unlocked } from '@/vault/operations'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { VaultMembersTab, VaultOverviewTab, VaultSecretsTab } from './VaultTabs'

// A WORKSPACE VAULT'S ONE PAGE, `/w/:workspace/vaults/:vaultId` — its key, what it
// serves, its members and its secrets, behind tabs (docs/architecture/pages.md § One
// editor per object). Settings → Secrets lists a workspace's vaults and links here; a
// runner's credentials link to the vault that holds them.
//
//   Overview  — its key (make it, give copies to whoever waits) and what it serves
//   Members   — who can open it, and why each person is one
//   Secrets   — the names it holds; add or replace one, sealed in this browser

export interface VaultOutletContext {
  vault: WorkspaceVaultOut
  /** A write changed it server-side: the page shows the fresh row. */
  onChange: (vault: WorkspaceVaultOut) => void
  /** Read it again (after a key-state write, which answers in another shape). */
  reload: () => void
  /** My vault key, unlocked, for a step that needs it. */
  unlock: () => Promise<Unlocked>
}

export type VaultTab = 'overview' | 'members' | 'secrets'

export function vaultTabs(vault: WorkspaceVaultOut): ObjectTab[] {
  return [
    { to: 'overview', label: 'Overview', dot: vault.kv === 0, dotLabel: 'no key yet' },
    { to: 'members', label: 'Members' },
    { to: 'secrets', label: 'Secrets' },
  ]
}

export function VaultPage(): JSX.Element {
  const { workspace = '', vaultId = '' } = useParams()
  const [vault, setVault] = useState<WorkspaceVaultOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const unlocker = useVaultUnlock()

  const load = useCallback(() => {
    setError(null)
    getWorkspaceVault(vaultId)
      .then(setVault)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load this vault'))
  }, [vaultId])
  useEffect(() => load(), [load])

  const back = (
    <Link to={scopedPath(workspace, '/settings/secrets')} className="text-primary hover:underline">All vaults →</Link>
  )
  if (error) {
    return (
      <div className="max-w-4xl">
        <p className="text-[12px]">{back}</p>
        <p className="mt-6 text-sm text-destructive" data-testid="vault-page-error">{error}</p>
      </div>
    )
  }
  if (!vault) return <div className="h-8 w-64 animate-pulse rounded-md bg-muted" data-testid="vault-page-loading" />
  if (vault.workspace !== workspace) return <Navigate to={scopedPath(vault.workspace, `/vaults/${vault.vault_id}`)} replace />

  const ctx: VaultOutletContext = { vault, onChange: setVault, reload: load, unlock: unlocker.unlock }
  return (
    <ObjectPage
      title={vault.name}
      context="Vault"
      status={
        <span className={`rounded px-1.5 py-0.5 text-[11px] ${vault.kv ? 'bg-success/15 text-success' : 'bg-warning/15 text-warning'}`}
          data-testid="vault-page-status">
          {vault.kv ? `key version ${vault.kv}` : 'no key yet'}
        </span>
      }
      subtitle={`A zero-knowledge vault in ${vault.workspace}: only its members, and the machines they work for, can open it — canopy-web never can.`}
      actions={back}
      tabsLabel="Vault"
      tabs={vaultTabs(vault)}
    >
      <Outlet context={ctx} />
      {unlocker.element}
    </ObjectPage>
  )
}

const TAB_COMPONENTS: Record<VaultTab, (p: VaultOutletContext) => JSX.Element> = {
  overview: VaultOverviewTab,
  members: VaultMembersTab,
  secrets: VaultSecretsTab,
}

/** Route element for `:tab`; an unknown tab opens Overview. */
export function VaultTabRoute(): JSX.Element {
  const ctx = useOutletContext<VaultOutletContext>()
  const { tab = '' } = useParams()
  const Tab = TAB_COMPONENTS[tab as VaultTab]
  if (!Tab) return <Navigate to="../overview" relative="path" replace />
  return <Tab {...ctx} />
}
