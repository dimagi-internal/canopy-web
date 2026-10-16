import { useCallback, useRef, useState, type ReactNode } from 'react'
import type { UnlockAsk, UnlockChoice, UnlockPrompt } from './operations'

/**
 * "Unlock your vault key" — passkey or passphrase (canopy-web#1550). A hook, so
 * each surface renders the prompt inline where the person is working:
 * `const { prompt, element } = useUnlockPrompt()`, pass `prompt` to the
 * operation, render `element`. The passphrase never leaves this component's
 * state except into the operation that derives a key from it.
 */
export function useUnlockPrompt(): { prompt: UnlockPrompt; element: ReactNode } {
  const [ask, setAsk] = useState<UnlockAsk | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const [secretKey, setSecretKey] = useState('')
  const pending = useRef<{ resolve: (c: UnlockChoice) => void; reject: (e: Error) => void } | null>(null)

  const prompt = useCallback<UnlockPrompt>((a) => new Promise<UnlockChoice>((resolve, reject) => {
    pending.current = { resolve, reject }
    setPassphrase('')
    setSecretKey('')
    setAsk(a)
  }), [])

  const finish = (choice: UnlockChoice | null) => {
    const p = pending.current
    pending.current = null
    setAsk(null)
    setPassphrase('')
    setSecretKey('')
    if (choice) p?.resolve(choice)
    else p?.reject(new Error('Unlock cancelled — nothing was changed.'))
  }

  const element = ask && (
    <form
      role="dialog"
      aria-label="Unlock your vault key"
      className="mt-3 space-y-2 rounded-lg border border-border bg-card p-3 text-[12px]"
      data-testid="vault-unlock"
      onSubmit={(e) => {
        e.preventDefault()
        finish({ method: 'passphrase', passphrase, secretKey: secretKey.trim() || undefined })
      }}
    >
      <p className="font-medium text-foreground">Unlock your vault key</p>
      {ask.passkey && (
        <button type="button" onClick={() => finish({ method: 'passkey' })} data-testid="vault-unlock-passkey"
                className="min-h-11 rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground sm:min-h-0">
          Use my passkey
        </button>
      )}
      {ask.passphrase && (
        <div className="flex flex-wrap items-center gap-2">
          <input type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)}
                 autoComplete="current-password" aria-label="Vault passphrase" placeholder="Passphrase"
                 className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-foreground sm:min-h-0 sm:w-64" />
          {ask.needsSecretKey && (
            <input value={secretKey} onChange={(e) => setSecretKey(e.target.value)} autoComplete="off"
                   spellCheck={false} aria-label="Secret Key" placeholder="CV1-…  (from your Emergency Kit)"
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-foreground sm:min-h-0 sm:w-72" />
          )}
          <button type="submit" disabled={!passphrase || (ask.needsSecretKey && !secretKey.trim())}
                  data-testid="vault-unlock-passphrase"
                  className="min-h-11 rounded-md border border-input px-3 py-1 text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
            Unlock
          </button>
        </div>
      )}
      <button type="button" onClick={() => finish(null)}
              className="text-muted-foreground hover:text-foreground">Cancel</button>
    </form>
  )
  return { prompt, element }
}
