import { useState } from 'react'
import { Button } from 'canopy-ui/ui'
import { EmergencyKit } from '@/components/settings/VaultKeyPanel'
import { createVaultKeyWithPassphrase, type Unlocked } from './operations'
import { passphraseProblem } from './zk'

/**
 * Create a vault key sealed by a passphrase + Secret Key, inline, wherever a
 * flow needs one (canopy-web#1550). The common route for a passkey kept in a
 * password manager, which gives no PRF. Shows the Secret Key ONCE, then hands
 * the new key back unlocked through `onReady`, so the flow continues without
 * asking for the passphrase it was just given.
 */
export function PassphraseSetup({ note, submitLabel, onReady, onCancel }: {
  /** One plain line above the form: why a passphrase. */
  note?: string
  submitLabel: string
  onReady: (me: Unlocked) => void
  onCancel?: () => void
}) {
  const [pass1, setPass1] = useState('')
  const [pass2, setPass2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [made, setMade] = useState<{ secretKey: string; me: Unlocked } | null>(null)
  const problem = pass1 ? passphraseProblem(pass1) : null
  const mismatch = Boolean(pass2) && pass1 !== pass2

  if (made) return <EmergencyKit secretKey={made.secretKey} email="" onDone={() => onReady(made.me)} />

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (problem || mismatch || !pass1) return
    setBusy(true)
    setError('')
    try {
      const out = await createVaultKeyWithPassphrase(pass1)
      setMade({ secretKey: out.secretKey, me: out.me })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create your vault key.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-2" data-testid="vault-passphrase-setup">
      {note && <p className="text-[12px] text-foreground-secondary" data-testid="vault-passphrase-note">{note}</p>}
      <div className="flex flex-wrap gap-2">
        <input type="password" value={pass1} onChange={(e) => setPass1(e.target.value)} autoComplete="new-password"
               aria-label="New vault passphrase" placeholder="Passphrase (12+ characters)"
               className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-64" />
        <input type="password" value={pass2} onChange={(e) => setPass2(e.target.value)} autoComplete="new-password"
               aria-label="Repeat the passphrase" placeholder="Repeat it"
               className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-64" />
      </div>
      {(problem || mismatch) && (
        <p className="text-xs text-warning">{problem ?? 'The two do not match.'}</p>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" type="submit" disabled={busy || !pass1 || !!problem || pass1 !== pass2}
                data-testid="vault-passphrase-setup-go">
          {busy ? 'Working…' : submitLabel}
        </Button>
        {onCancel && (
          <Button size="sm" type="button" variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
        )}
      </div>
    </form>
  )
}
