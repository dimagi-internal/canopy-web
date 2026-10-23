import { useEffect, useState, type JSX } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { createWorkspaceVault, listWorkspaceVaults, type WorkspaceVaultOut } from '@/api/vault'
import { vaultHref } from '@/lib/scopedLinks'

// A workspace's own vaults (spec 2026-10-22-vault-access-follows-canopy-access-design.md
// §4): shared secrets its agents read, layered under each agent's own, and a runner's own
// Claude credentials. One row each here; everything about one vault — its key, what it
// serves, members, secrets — is on its own page (pages/vault/VaultPage.tsx).

function serves(v: WorkspaceVaultOut): string {
  if (v.attachments.length === 0) return 'nothing yet'
  return v.attachments
    .map((a) => (a.runner ? `runner ${a.runner_name ?? a.runner}` : a.agent ? a.agent : `every agent in ${a.workspace} and below`))
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
            <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
              <th className="py-1 font-normal">Vault</th><th className="py-1 font-normal">Key</th>
              <th className="py-1 font-normal">Secrets</th><th className="py-1 font-normal">Serves</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {vaults.map((v) => (
              <tr key={v.vault_id} data-testid={`ws-vault-${v.vault_id}`}>
                <td className="py-1.5">
                  <Link to={vaultHref(v.workspace, v.vault_id)} className="font-medium text-primary hover:underline">{v.name}</Link>
                </td>
                <td className={`py-1.5 ${v.kv ? 'text-muted-foreground' : 'text-warning'}`}>{v.kv ? `version ${v.kv}` : 'none yet'}</td>
                <td className="py-1.5 text-muted-foreground">{v.secrets.length}</td>
                <td className="py-1.5 text-muted-foreground">{serves(v)}</td>
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
