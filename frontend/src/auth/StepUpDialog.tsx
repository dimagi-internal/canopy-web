import { useEffect, useRef, useState, type JSX } from 'react'
import { Button, Dialog, DialogContent, DialogDescription, DialogTitle, Input } from 'canopy-ui'
import { performStepUp, registerThisDevice } from '@/api/passkeys'
import { setStepUpHandler, type StepUpRequest } from './stepUp'
import { defaultLabel, webauthnSupported } from './webauthn'

/**
 * The prompt behind every passkey step-up (canopy-web#1538). Mounted once at
 * the app root; `stepUp.ts`'s middleware opens it when a request comes back
 * `step_up_required`, and retries that request when it resolves.
 *
 * A person with no passkey yet is offered to register THIS device right here —
 * registering verifies them, which opens the step-up window too, so the
 * original request then goes through without a second prompt.
 */
export function StepUpDialog(): JSX.Element {
  const [req, setReq] = useState<StepUpRequest | null>(null)
  const [label, setLabel] = useState(defaultLabel())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const settle = useRef<{ resolve: () => void; reject: (e: Error) => void } | null>(null)

  useEffect(() => {
    setStepUpHandler(
      (next) =>
        new Promise<void>((resolve, reject) => {
          settle.current = { resolve, reject }
          setError('')
          setBusy(false)
          setReq(next)
        }),
    )
    return () => setStepUpHandler(null)
  }, [])

  const close = (ok: boolean) => {
    const s = settle.current
    settle.current = null
    setReq(null)
    if (ok) s?.resolve()
    else s?.reject(new Error('Passkey confirmation cancelled'))
  }

  const run = async () => {
    setBusy(true)
    setError('')
    try {
      if (req?.hasPasskey) await performStepUp()
      else await registerThisDevice(label.trim() || defaultLabel())
      close(true)
    } catch (e) {
      // NotAllowedError = the person dismissed the browser's own prompt.
      setError(e instanceof Error && e.name !== 'NotAllowedError' ? e.message : 'The passkey prompt was dismissed.')
      setBusy(false)
    }
  }

  const supported = webauthnSupported()
  return (
    <Dialog open={req !== null} onOpenChange={(o: boolean) => !o && !busy && close(false)}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-md bg-card" data-testid="step-up-dialog">
        <DialogTitle className="text-sm font-semibold text-foreground">
          {req?.hasPasskey ? 'Confirm it’s you' : 'Set up a passkey for this device'}
        </DialogTitle>
        <DialogDescription className="text-[13px] text-foreground-secondary">
          {req?.hasPasskey
            ? 'This action handles a secret, so it needs your passkey — Touch ID, your device password or PIN, your phone, or a security key. One confirmation covers the next five minutes.'
            : 'This action handles a secret, so it needs a passkey, and you have none yet. Register this device — Touch ID, your device password or PIN, your phone, or a security key — and the action continues.'}
        </DialogDescription>
        {!supported && (
          <p className="text-[13px] text-destructive" data-testid="step-up-unsupported">
            This browser does not support passkeys. Use a current Chrome, Safari, Edge or Firefox.
          </p>
        )}
        {supported && !req?.hasPasskey && (
          <div>
            <label htmlFor="step-up-label" className="mb-1 block text-xs text-muted-foreground">
              Name this device
            </label>
            <Input id="step-up-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={120} />
          </div>
        )}
        {error && (
          <p className="text-[13px] text-destructive" data-testid="step-up-error">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={() => close(false)} disabled={busy}>
            Cancel
          </Button>
          <Button type="button" size="sm" onClick={() => void run()} disabled={busy || !supported} data-testid="step-up-go">
            {busy ? 'Waiting for your passkey…' : req?.hasPasskey ? 'Use passkey' : 'Register this device'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
