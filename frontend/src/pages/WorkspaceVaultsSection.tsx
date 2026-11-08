import { useEffect, useState, type JSX } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { createWorkspaceVault, listWorkspaceVaults, type WorkspaceVaultOut } from '@/api/vault'
import { vaultHref } from '@/lib/scopedLinks'
import { Td, Th, Tr } from '@/components/DataTable'

// A workspace's own vaults: shared secrets its agents read, layered under each agent's own, and a runner's own
// Claude credentials. One row each here; everything about one vault — its key, what it
// serves, members, secrets — is on its own page (pages/vault/VaultPage.tsx).

function serves(v: WorkspaceVaultOut): string {
  if (v.attachments.length === 0) return 'nothing yet'
  return v.attachments
    .map((a) => (a.runner ? `runner ${a.runner_name ?? a.runner}` : a.project ? `project ${a.project}` : a.agent ? a.agent : `every agent in ${a.workspace} and below`))
    .join('; ')
}

export function WorkspaceVaultsSection({ slug }: { slug: string }): JSX.Element | null {
  const [vaults, setVaults] = useState<WorkspaceVaultOut[] | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    let off = false
    listWorkspaceVaults(slug).then((v) => !off && setVaults(v)).catch(() => !off && setVaults(null))
    return () => {
      off = true
    }
  }, [slug])

  if (vaults === null) return null

  return (
    <section className="rounded-lg border border-border bg-card p-3" data-testid="workspace-vaults">
      <h2 className="mb-1 text-[13px] font-medium text-foreground">Workspace vaults</h2>
      <p className="mb-2 text-[12px] text-muted-foreground">
        Secrets this workspace shares with its agents, read under each agent&rsquo;s own (an agent&rsquo;s own secret wins),
        and runners&rsquo; own Claude credentials. Only members can open one, and canopy-web never can.
      </p>
      {vaults.length === 0 && <p className="text-[12px] text-muted-foreground">No vaults yet.</p>}
      {vaults.length > 0 && (
        <table className="w-full text-[12px]" data-testid="ws-vault-list">
          <thead>
            <Tr head="label">
              <Th weight="normal">Vault</Th><Th weight="normal">Key</Th>
              <Th weight="normal">Secrets</Th><Th weight="normal">Serves</Th>
            </Tr>
          </thead>
          <tbody className="divide-y divide-border">
            {vaults.map((v) => (
              <tr key={v.vault_id} data-testid={`ws-vault-${v.vault_id}`}>
                <Td density="cozy">
                  <Link to={vaultHref(v.workspace, v.vault_id)} className="font-medium text-primary hover:underline">{v.name}</Link>
                </Td>
                <Td density="cozy" className={v.kv ? 'text-muted-foreground' : 'text-warning'}>{v.kv ? `version ${v.kv}` : 'none yet'}</Td>
                <Td density="cozy" tone="muted">{v.secrets.length}</Td>
                <Td density="cozy" tone="muted">{serves(v)}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form className="mt-3 flex flex-wrap items-center gap-2" data-testid="ws-vault-create"
            onSubmit={(e) => {
              e.preventDefault()
              setError(null)
              createWorkspaceVault(slug, name.trim())
                .then((v) => navigate(vaultHref(v.workspace, v.vault_id)))
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
