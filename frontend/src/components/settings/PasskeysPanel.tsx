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

  const synced = (keys ?? []).some((k) => k.backed_up)
  return (
    // A row of the Security card: what your passkey is for, then each one as
    // one plain line. Adding another is tucked below — most people need one.
    <div id="passkeys" className="space-y-2" data-testid="settings-passkeys">
      <p className="text-xs text-muted-foreground" data-testid="passkeys-explain">
        <span className="text-foreground">Your passkey</span>
        {synced ? ' (saved in your password manager)' : ''} — confirms it&rsquo;s you and unlocks your agents&rsquo;
        secrets in this browser.
      </p>

      {!supported && <p className="text-sm text-destructive">This browser does not support passkeys.</p>}

      {keys === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : keys.length === 0 ? (
        <p className="text-sm text-muted-foreground">No passkeys yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {keys.map((k) => (
            <li key={k.id} className="flex items-start justify-between gap-3 py-1.5" data-testid={`passkey-${k.id}`}>
              <div className="min-w-0 text-[13px]">
                <p className="text-foreground">
                  {k.label || 'passkey'}
                  {sealedBy?.has(k.credential_id) && (
                    <span className="ml-2 text-success" data-testid={`passkey-unlocks-vault-${k.id}`}>
                      opens your agents&rsquo; secrets ✓
                    </span>
                  )}
                </p>
                <p className="text-[12px] text-muted-foreground">
                  {k.backed_up ? 'In your password manager (synced)' : 'On this device only'} · added{' '}
                  {k.created_at.slice(0, 10)} · {k.last_used_at ? `last used ${relativeAge(k.last_used_at)}` : 'never used'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void onDelete(k)}
                disabled={busy}
                className="shrink-0 text-[12px] text-destructive hover:underline disabled:opacity-40"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
      {sealedBy !== null && hasPassphrase && (
        <p className="text-xs text-muted-foreground" data-testid="passkeys-vault-status">
          Your passphrase and Secret Key also open your agents&rsquo; secrets.
        </p>
      )}

      {!supported ? null : keys !== null && keys.length > 0 && !adding ? (
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
    </div>
  )
}
