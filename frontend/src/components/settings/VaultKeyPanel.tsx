import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'
import { getKeyset, type VaultKeysetOut } from '@/api/vault'
import {
  addPassphraseToVaultKey,
  addPasskeyToVaultKey,
  setUpVaultKey,
  setUpVaultKeyWithPassphrase,
} from '@/vault/operations'
import { browserSupportsPrf, PrfUnsupportedError, PRF_UNSUPPORTED } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'
import { passphraseProblem } from '@/vault/zk'
import { webauthnSupported } from '@/auth/webauthn'

/**
 * Your vault key (Canopy Vault zero-knowledge, canopy-web#1550): a key pair made
 * in this browser that zero-knowledge agents wrap their vault key to. Its
 * private half is kept only SEALED — by a passkey (its PRF extension), and/or
 * by a passphrase plus a Secret Key (1Password-style). canopy-web stores the
 * seals and cannot open them; it never sees the passphrase or the Secret Key.
 */

function emergencyKit(email: string, secretKey: string): string {
  return `Canopy vault — Emergency Kit\nAccount: ${email}\n\nSecret Key: ${secretKey}\n\n` +
    'With your vault passphrase, this Secret Key opens your vault key on a new device. canopy-web does not keep a ' +
    'copy. Store it somewhere safe and offline; never with your passphrase.\n'
}

export function EmergencyKit({ secretKey, email, onDone }: { secretKey: string; email: string; onDone: () => void }) {
  const [saved, setSaved] = useState(false)
  const text = emergencyKit(email, secretKey)
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'canopy-vault-emergency-kit.txt'
    a.click()
    URL.revokeObjectURL(url)
  }
  const print = () => {
    const w = window.open('', '_blank')
    if (!w) return
    const pre = w.document.createElement('pre')
    pre.textContent = text
    w.document.body.appendChild(pre)
    w.print()
  }
  return (
    <div role="dialog" aria-label="Emergency Kit" data-testid="vault-emergency-kit"
         className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
      <p className="font-medium text-foreground">Your Secret Key — shown once</p>
      <p className="text-muted-foreground">
        This browser remembers it, so here you need only your passphrase. On a new device you will need both. Download
        or print your Emergency Kit now.
      </p>
      <code className="block select-all rounded border border-border bg-card px-2 py-1 font-mono text-foreground"
            data-testid="vault-secret-key">{secretKey}</code>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={download}>Download Emergency Kit</Button>
        <Button size="sm" variant="outline" onClick={print}>Print</Button>
        <label className="flex items-center gap-1 text-foreground">
          <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="vault-kit-saved" />
          I have saved it
        </label>
        <Button size="sm" disabled={!saved} onClick={onDone} data-testid="vault-kit-done">Done</Button>
      </div>
    </div>
  )
}

export function VaultKeyPanel({ email = '' }: { email?: string }) {
  const [ks, setKs] = useState<VaultKeysetOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [prfHint, setPrfHint] = useState<boolean | null>(null)
  const [mode, setMode] = useState<'passphrase' | null>(null)
  const [pass1, setPass1] = useState('')
  const [pass2, setPass2] = useState('')
  const [kit, setKit] = useState<string | null>(null)
  const { prompt, element } = useUnlockPrompt()

  useEffect(() => {
    let cancelled = false
    getKeyset()
      .then((k) => !cancelled && setKs(k))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load your vault key.'))
    void browserSupportsPrf().then((v) => !cancelled && setPrfHint(v))
    return () => {
      cancelled = true
    }
  }, [])

  async function run(fn: () => Promise<VaultKeysetOut | { keyset: VaultKeysetOut; secretKey: string }>) {
    setBusy(true)
    setError('')
    try {
      const out = await fn()
      if ('keyset' in out) {
        setKs(out.keyset)
        setKit(out.secretKey)
        setMode(null)
        setPass1('')
        setPass2('')
      } else {
        setKs(out)
      }
    } catch (e) {
      if (e instanceof PrfUnsupportedError) setError(PRF_UNSUPPORTED)
      else if (e instanceof Error && e.name === 'NotAllowedError') setError('The passkey prompt was dismissed.')
      else setError(e instanceof Error ? e.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  const passkeyOk = webauthnSupported() && prfHint !== false
  const sealsOf = (k: VaultKeysetOut) => ((k.seals ?? []) as { alg?: string }[]).map((s) => s.alg)
  const hasPassphrase = ks ? sealsOf(ks).includes('2skd') : false
  const unsealed = (ks?.passkeys ?? []).filter((p) => !p.sealed)
  const problem = pass1 ? passphraseProblem(pass1) : null
  const mismatch = pass2 && pass1 !== pass2

  const submitPassphrase = (e: React.FormEvent) => {
    e.preventDefault()
    if (problem || mismatch || !pass1) return
    const replace = Boolean(ks?.exists)
    if (ks?.exists && ks.public_jwk) void run(() => addPassphraseToVaultKey(prompt, pass1))
    else void run(() => setUpVaultKeyWithPassphrase(pass1, replace))
  }

  const passphraseForm = (
    <form onSubmit={submitPassphrase} className="space-y-2" data-testid="vault-passphrase-form">
      <div className="flex flex-wrap gap-2">
        <input type="password" value={pass1} onChange={(e) => setPass1(e.target.value)} autoComplete="new-password"
               aria-label="New vault passphrase" placeholder="Passphrase (12+ characters)"
               className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-64" />
        <input type="password" value={pass2} onChange={(e) => setPass2(e.target.value)} autoComplete="new-password"
               aria-label="Repeat the passphrase" placeholder="Repeat it"
               className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-64" />
        <Button size="sm" type="submit" disabled={busy || !pass1 || !!problem || !!mismatch || pass1 !== pass2}
                data-testid="vault-passphrase-save">
          {busy ? 'Working…' : 'Save passphrase'}
        </Button>
      </div>
      {(problem || mismatch) && (
        <p className="text-xs text-warning" data-testid="vault-passphrase-problem">{problem ?? 'The two do not match.'}</p>
      )}
      <p className="text-xs text-muted-foreground">
        You will also get a Secret Key: canopy-web never sees either, so a stolen database cannot guess your passphrase.
      </p>
    </form>
  )

  return (
    <div id="vault-key" className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-vault-key">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Vault key</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          For agents with zero-knowledge on: their secrets are encrypted in your browser, and canopy-web stores only
          ciphertext. Your vault key is made here and kept sealed by your passkey and/or a passphrase with a Secret
          Key; canopy-web cannot open it.
        </p>
      </div>

      {kit && <EmergencyKit secretKey={kit} email={email} onDone={() => setKit(null)} />}

      {ks === null ? (
        <p className="text-sm text-muted-foreground">{error || 'Loading…'}</p>
      ) : !ks.exists ? (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">Not set up yet. Choose how to protect it — you can add the other later.</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy || !passkeyOk || (ks.passkeys ?? []).length === 0}
                    onClick={() => void run(() => setUpVaultKey())} data-testid="vault-key-setup">
              {busy && mode === null ? 'Waiting for your passkey…' : 'Use my passkey (recommended)'}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setMode('passphrase')}
                    data-testid="vault-key-setup-passphrase">
              Use a passphrase
            </Button>
          </div>
          {!passkeyOk ? (
            <p className="text-xs text-muted-foreground" data-testid="vault-key-unsupported">{PRF_UNSUPPORTED} Or use a passphrase.</p>
          ) : (ks.passkeys ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">A passkey seal needs a passkey registered above.</p>
          ) : null}
          {mode === 'passphrase' && passphraseForm}
        </div>
      ) : (
        <div className="space-y-2">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">Unlocks with</th>
                <th className="py-1 font-medium">PRF</th>
                <th className="py-1 font-medium">Holds your vault key</th>
              </tr>
            </thead>
            <tbody>
              {(ks.passkeys ?? []).map((p) => (
                <tr key={p.credential_id} className="border-t border-border" data-testid={`vault-passkey-${p.credential_id}`}>
                  <td className="py-1.5 text-foreground-secondary">Passkey · {p.label || 'passkey'}</td>
                  <td className="py-1.5 text-muted-foreground">
                    {p.prf_enabled === true ? 'supported' : p.prf_enabled === false ? 'not supported' : 'unknown'}
                  </td>
                  <td className={`py-1.5 ${p.sealed ? 'text-success' : 'text-muted-foreground'}`}>{p.sealed ? 'sealed' : '—'}</td>
                </tr>
              ))}
              <tr className="border-t border-border" data-testid="vault-passphrase-row">
                <td className="py-1.5 text-foreground-secondary">Passphrase + Secret Key</td>
                <td className="py-1.5 text-muted-foreground">—</td>
                <td className={`py-1.5 ${hasPassphrase ? 'text-success' : 'text-muted-foreground'}`}>
                  {hasPassphrase ? 'sealed' : '—'}
                </td>
              </tr>
            </tbody>
          </table>
          <div className="flex flex-wrap gap-2">
            {unsealed.length > 0 && passkeyOk && (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => addPasskeyToVaultKey(prompt))}
                      data-testid="vault-key-add-passkey">
                Add a passkey to it
              </Button>
            )}
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setMode(mode ? null : 'passphrase')}
                    data-testid="vault-key-add-passphrase">
              {hasPassphrase ? 'Change passphrase' : 'Add a passphrase'}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} data-testid="vault-key-replace"
                    onClick={() => {
                      if (window.confirm('Start a NEW vault key? Every zero-knowledge agent must grant you again (or you recover with its code).')) {
                        void run(() => setUpVaultKey(true))
                      }
                    }}>
              Start a new vault key
            </Button>
          </div>
          {mode === 'passphrase' && passphraseForm}
          {element}
        </div>
      )}
      {error && ks !== null && <p className="text-sm text-destructive" data-testid="vault-key-error">{error}</p>}
    </div>
  )
}
