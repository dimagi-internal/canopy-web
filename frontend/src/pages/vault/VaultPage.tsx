import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { getWorkspaceVault, type WorkspaceVaultOut } from '@/api/vault'
import { ObjectPage } from '@/components/objectPage/ObjectPage'
import { VaultView } from '@/components/vault/VaultView'
import { scopedPath } from '@/lib/scopedLinks'
import type { Unlocked } from '@/vault/operations'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { VaultMembersTab, VaultOverviewTab, VaultSecretsTab } from './VaultTabs'

// A WORKSPACE VAULT'S ONE PAGE, `/w/:workspace/vaults/:vaultId`, drawn by the same
// `VaultView` an agent's vault uses (components/vault/VaultView.tsx): a summary
// line, then Secrets, Who can open them and Storage & key as collapsible sections
// (docs/architecture/pages.md § One editor per object). Settings → Secrets lists a
// workspace's vaults and links here; a runner's credentials link to the vault that
// holds them. This file is the workspace vault's ADAPTER: it reads the vault and
// gives the frame each section's body and one-line summary.
//
// The old tab addresses (`/overview`, `/members`, `/secrets`) still work: each opens
// the section that tab became.

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
  const runners = vault.attachments.filter((a) => a.runner).length
  const waiting = vault.members.filter((m) => m.has_vault_key && !m.has_copy).length
  const noKey = vault.kv === 0
  const attention = [
    noKey && 'no key yet, so nothing can go in',
    !noKey && waiting > 0 && `${waiting} ${waiting === 1 ? 'person is' : 'people are'} waiting for the key`,
  ].filter((a): a is string => Boolean(a))
  const people = `${vault.members.length} ${vault.members.length === 1 ? 'person' : 'people'}`

  return (
    <ObjectPage
      title={vault.name}
      context="Vault"
      subtitle={`A workspace vault in ${vault.workspace}: only its members, and the machines they work for, can open it — canopy-web never can.`}
      actions={back}
    >
      <VaultView
        facts={{
          where: 'Canopy vault',
          encrypted: true,
          keyVersion: vault.kv,
          secrets: vault.secrets.length,
          people: vault.members.length,
          machines: runners,
          attention,
        }}
        secrets={{
          summary: `${vault.secrets.length} stored`,
          tone: 'ok',
          defaultOpen: !tab || tab === 'secrets',
          body: <VaultSecretsTab {...ctx} />,
        }}
        access={{
          summary: `${people} · ${runners} ${runners === 1 ? 'machine' : 'machines'}`,
          tone: waiting > 0 ? 'attention' : 'quiet',
          defaultOpen: tab === 'members',
          body: <VaultMembersTab {...ctx} />,
        }}
        storage={{
          summary: noKey ? 'no key yet' : `key version ${vault.kv}`,
          tone: noKey ? 'attention' : 'quiet',
          defaultOpen: tab === 'overview',
          body: <VaultOverviewTab {...ctx} />,
        }}
      />
      {unlocker.element}
    </ObjectPage>
  )
}
