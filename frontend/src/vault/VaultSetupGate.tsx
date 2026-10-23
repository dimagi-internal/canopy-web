import { useEffect, useState, type ReactNode } from 'react'
import { Button } from 'canopy-ui/ui'
import { getKeyset, getVaultSetup } from '@/api/vault'
import { CanopyWordmark } from '@/brand/CanopyWordmark'
import { registerThisDevice } from '@/api/passkeys'
import { defaultLabel, webauthnSupported } from '@/auth/webauthn'
import { vaultKeyForFlow } from './operations'
import { PassphraseSetup } from './PassphraseSetup'
import { browserSupportsPrf } from './prf'
import { useUnlockPrompt } from './UnlockPrompt'

/**
 * Everyone who signs in sets up a passkey and a vault key once, before
 * anything else. A vault member with no vault key is one no keeper can pass a
 * key to, so doing it here means being added to an agent later just works.
 *
 * The server decides who is asked (`GET /api/vault/setup`): a person at a
 * browser with no vault key yet. A token-minted session never is.
 */

const SKIP_KEY = 'canopy.vaultSetup.skipped'

function skippedThisTab(): boolean {
  try {
    return sessionStorage.getItem(SKIP_KEY) === '1'
  } catch {
    return false
  }
}

export function VaultSetupGate({ children }: { children: ReactNode }) {
  const [required, setRequired] = useState<boolean | null>(null)
  const [hasPasskey, setHasPasskey] = useState(false)

  useEffect(() => {
    let cancelled = false
    if (skippedThisTab()) {
      setRequired(false)
      return
    }
    // A failed check never locks anyone out of the app.
    getVaultSetup()
      .then((s) => {
        if (cancelled) return
        setHasPasskey(s.has_passkey)
        setRequired(s.required)
      })
      .catch(() => !cancelled && setRequired(false))
    return () => {
      cancelled = true
    }
  }, [])

  if (required === null) {
    return (
      <div className="min-h-screen bg-background text-muted-foreground flex items-center justify-center text-sm">
        Loading…
      </div>
    )
  }
  if (!required) return <>{children}</>
  return <VaultSetupScreen hasPasskey={hasPasskey} onDone={() => setRequired(false)} />
}

export function VaultSetupScreen({ hasPasskey, onDone }: { hasPasskey: boolean; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [passkeyMade, setPasskeyMade] = useState(hasPasskey)
  const [error, setError] = useState('')
  const [passphrase, setPassphrase] = useState(false)
  const { prompt, element } = useUnlockPrompt()
  const supported = webauthnSupported()

  const skip = () => {
    try {
      sessionStorage.setItem(SKIP_KEY, '1')
    } catch {
      // private window: skipping still works for this page load
    }
    onDone()
  }

  /** Saving a vault key takes a passkey confirmation, so even a key sealed by
   *  a passphrase needs a passkey on the account first (any kind will do). */
  const ensurePasskey = async () => {
    const ks = await getKeyset()
    if ((ks.passkeys ?? []).length > 0) return
    await registerThisDevice(defaultLabel())
  }

  const toPassphrase = async () => {
    if (!passkeyMade) {
      await ensurePasskey()
      setPasskeyMade(true)
    }
    setPassphrase(true)
  }

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError('')
    try {
      await fn()
    } catch (e) {
      setError(e instanceof Error && e.name === 'NotAllowedError'
        ? 'The passkey prompt was dismissed. Try again when you are ready.'
        : e instanceof Error ? e.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  const start = () => run(async () => {
    // Registers a passkey that can hold the key when there is none, then
    // makes the key with it; null when only a passphrase can seal it here.
    const made = await vaultKeyForFlow(await browserSupportsPrf(), prompt)
    if (made) return onDone()
    await toPassphrase()
  })

  return (
    <div className="min-h-screen bg-background text-foreground-secondary flex items-center justify-center px-4">
      <div className="max-w-lg w-full bg-card border border-border rounded-xl p-6 space-y-4" data-testid="vault-setup">
        <h1 className="text-xl font-semibold text-foreground"><CanopyWordmark /></h1>
        <div className="space-y-2">
          <h2 className="text-base font-semibold text-foreground">Set up your passkey and vault key</h2>
          <p className="text-sm">
            Agents keep their secrets in Canopy Vault. Your vault key is how they let you in: once you have one,
            being added to an agent gives you its secrets with nothing else to do. Your passkey (Touch ID, Windows
            Hello or your phone) unlocks it; canopy-web never can.
          </p>
          <p className="text-sm text-muted-foreground">You do this once. It takes one or two touches.</p>
        </div>

        {!supported ? (
          <div className="space-y-2" data-testid="vault-setup-unsupported">
            <p className="text-sm text-warning">
              This browser cannot make a passkey. Sign in from Chrome, Safari, Edge or Firefox on your computer or
              phone to set it up.
            </p>
            <Button size="sm" variant="ghost" onClick={skip} data-testid="vault-setup-skip">Continue for now</Button>
          </div>
        ) : passphrase ? (
          <PassphraseSetup
            note="Your passkey cannot hold a vault key on this browser (many password managers can't). Protect it with a passphrase instead."
            submitLabel="Create my vault key"
            onReady={onDone}
          />
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy} onClick={() => void start()} data-testid="vault-setup-go">
              {busy ? 'Waiting for your passkey…' : 'Set up with my passkey'}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(toPassphrase)}
                    data-testid="vault-setup-passphrase">
              Use a passphrase
            </Button>
          </div>
        )}

        {error && (
          <div className="space-y-2">
            <p className="text-sm text-destructive" data-testid="vault-setup-error">{error}</p>
            <Button size="sm" variant="ghost" onClick={skip} data-testid="vault-setup-skip">Continue for now</Button>
          </div>
        )}
        {element}
      </div>
    </div>
  )
}
