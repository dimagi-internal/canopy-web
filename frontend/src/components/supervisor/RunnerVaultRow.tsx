import { useEffect, useState, type JSX } from 'react'
import { type CredentialStatus } from '@/api/harness'
import { getWorkspaceVaultZk } from '@/api/vault'
import { grantWorkspaceVaultPending, makeWorkspaceVaultKey, type Unlocked } from '@/vault/operations'
import { Link } from 'react-router-dom'
import { vaultHref } from '@/lib/scopedLinks'
import { Button } from 'canopy-ui/ui'

/** Where the runner's tokens and keys are kept. In its own vault, a token is sealed with
 *  a key only this box and the vault's people hold; canopy-web keeps ciphertext. Its
 *  owner makes the key here, once, with a passkey touch, and lets new boxes in. */
export function RunnerVaultRow({ runnerId, workspace, vaults, busy, run, create, unlock }: {
  runnerId: string
  workspace: string | null
  vaults: NonNullable<CredentialStatus['vaults']>
  busy: boolean
  run: (fn: () => Promise<unknown>, fallback: string) => Promise<boolean>
  create: () => Promise<unknown>
  unlock: () => Promise<Unlocked>
}): JSX.Element {
  const vault = vaults[0] ?? null
  const [waiting, setWaiting] = useState(0)

  useEffect(() => {
    let live = true
    if (vault && vault.kv > 0) {
      // Who is waiting for a copy of the key (a new box, someone just added). Only a
      // manager of the vault can see it, and only a manager could grant it.
      getWorkspaceVaultZk(vault.vault_id)
        .then((z) => { if (live) setWaiting(z.pending.length) })
        .catch(() => { if (live) setWaiting(0) })
    }
    return () => { live = false }
  }, [vault, runnerId])

  if (!vault) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-dashed border-border p-2 text-[12px]"
        data-testid="runner-vault-none">
        <span className="text-muted-foreground">Tokens and keys are kept by canopy. Keep them in a vault only this runner can open instead.</span>
        <Button type="button" size="sm" disabled={busy} data-testid="runner-vault-create"
          onClick={() => void run(create, 'Failed to make the vault')}>Keep them in a vault</Button>
      </div>
    )
  }
  if (vault.kv === 0) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 p-2 text-[12px]"
        data-testid="runner-vault-no-key">
        <span className="text-foreground">{vault.name} has no key yet. Your passkey makes it: a copy for you and one for this runner.</span>
        <Button type="button" size="sm" disabled={busy} data-testid="runner-vault-make-key"
          onClick={() => void run(async () => makeWorkspaceVaultKey(vault.vault_id, await unlock()),
            'Failed to make the vault’s key')}>Make its key</Button>
      </div>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-[12px]" data-testid="runner-vault">
      <span className="text-success">●</span>
      {workspace
        ? <Link to={vaultHref(workspace, vault.vault_id)} className="text-primary hover:underline" data-testid="runner-vault-link">{vault.name}</Link>
        : <span className="text-foreground">{vault.name}</span>}
      <span className="text-[11px] text-foreground-subtle">opened only by this runner&rsquo;s own key</span>
      {waiting > 0 && (
        <Button type="button" size="sm" variant="outline" disabled={busy} data-testid="runner-vault-grant"
          onClick={() => void run(async () => {
            await grantWorkspaceVaultPending(vault.vault_id, await unlock())
            setWaiting(0)
          }, 'Failed to give out the vault’s key')}>
          Give {waiting === 1 ? 'the one waiting' : `the ${waiting} waiting`} a copy
        </Button>
      )}
    </div>
  )
}
