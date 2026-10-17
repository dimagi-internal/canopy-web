import { useEffect, useState } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import { deletePasskey, listPasskeys, registerThisDevice, type PasskeyOut } from '@/api/passkeys'
import { getKeyset } from '@/api/vault'
import { defaultLabel, webauthnSupported } from '@/auth/webauthn'
import { relativeAge } from '@/lib/relativeAge'

/**
 * Your passkeys (canopy-web#1538) — never a way to sign in; the confirmation
 * canopy asks for before an action that handles a secret (revealing or setting
 * an agent credential, approving a runner's device key).
 *
 * Your FIRST passkey needs only this signed-in session. Adding another, or
 * deleting one, asks for a passkey you already hold — the API client's step-up
 * prompt handles that and retries, so nothing here special-cases it.
 */
export function PasskeysPanel() {
  const [keys, setKeys] = useState<PasskeyOut[] | null>(null)
  const [label, setLabel] = useState(defaultLabel())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [adding, setAdding] = useState(false)
  /** Credential ids whose passkey holds a seal of the vault key; null = no vault key. */
  const [sealedBy, setSealedBy] = useState<Set<string> | null>(null)
  const [hasPassphrase, setHasPassphrase] = useState(false)
  const supported = webauthnSupported()

  async function refresh() {
    try {
      setKeys(await listPasskeys())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load passkeys.')
      setKeys([])
    }
    try {
      const ks = await getKeyset()
      setSealedBy(ks.exists ? new Set((ks.passkeys ?? []).filter((p) => p.sealed).map((p) => p.credential_id)) : null)
      setHasPassphrase(((ks.seals ?? []) as { alg?: string }[]).some((s) => s.alg === '2skd'))
    } catch {
      setSealedBy(null) // the vault status is a nicety; the passkey list stands without it
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function onRegister(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await registerThisDevice(label.trim() || defaultLabel())
      setAdding(false)
      await refresh()
    } catch (err) {
      setError(err instanceof Error && err.name !== 'NotAllowedError' ? err.message : 'The passkey prompt was dismissed.')
    } finally {
      setBusy(false)
    }
  }

  async function onDelete(k: PasskeyOut) {
    if (!window.confirm(`Delete the passkey "${k.label || 'passkey'}"? You will not be able to confirm with it again.`)) return
    setBusy(true)
    setError('')
    try {
      await deletePasskey(k.id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete that passkey.')
    } finally {
      setBusy(false)
    }
  }

  return (
    // A row of the Security card: how you confirm it's you (Touch ID, a
    // device PIN, a phone, a security key). One confirmation covers 5 minutes.
    <div id="passkeys" className="space-y-3" data-testid="settings-passkeys">

      {!supported ? (
        <p className="text-sm text-destructive">This browser does not support passkeys.</p>
      ) : keys !== null && keys.length > 0 && !adding ? (
        <button type="button" onClick={() => setAdding(true)} className="text-xs text-primary hover:underline"
                data-testid="passkey-add-another">
          Add another passkey
        </button>
      ) : keys === null ? null : (
        <form onSubmit={onRegister} className="flex flex-wrap items-end gap-2">
          <div className="min-w-[200px] flex-1">
            <label htmlFor="passkey-label" className="mb-1 block text-xs text-muted-foreground">
              Name this device
            </label>
            <Input id="passkey-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={120} />
          </div>
          <Button type="submit" size="sm" disabled={busy} data-testid="passkey-register">
            {busy ? 'Waiting for your passkey…' : 'Register this device'}
          </Button>
        </form>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {keys === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : keys.length === 0 ? (
        <p className="text-sm text-muted-foreground">No passkeys yet.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Passkey</th>
              <th className="py-1 font-medium">Added</th>
              <th className="py-1 font-medium">Last used</th>
              <th className="py-1 font-medium">Kind</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id} className="border-t border-border" data-testid={`passkey-${k.id}`}>
                <td className="py-1.5 text-foreground-secondary">
                  {k.label || 'passkey'}
                  {sealedBy?.has(k.credential_id) && (
                    <span className="ml-2 text-success" data-testid={`passkey-unlocks-vault-${k.id}`}>Unlocks your vault ✓</span>
                  )}
                </td>
                <td className="py-1.5 text-muted-foreground">{k.created_at.slice(0, 10)}</td>
                <td className="py-1.5 text-muted-foreground">{k.last_used_at ? relativeAge(k.last_used_at) : 'never'}</td>
                <td className="py-1.5 text-muted-foreground">{k.backed_up ? 'synced' : 'this device only'}</td>
                <td className="py-1.5 text-right">
                  <button
                    type="button"
                    onClick={() => void onDelete(k)}
                    disabled={busy}
                    className="text-destructive hover:underline disabled:opacity-40"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {sealedBy !== null && hasPassphrase && (
        <p className="text-xs text-muted-foreground" data-testid="passkeys-vault-status">
          Your passphrase and Secret Key also unlock your vault.
        </p>
      )}
    </div>
  )
}
