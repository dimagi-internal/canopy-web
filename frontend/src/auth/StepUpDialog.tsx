import { useEffect, useRef, useState, type JSX } from 'react'
import { Button, Dialog, DialogContent, DialogDescription, DialogTitle, Input } from 'canopy-ui'
import { performStepUp, registerThisDevice } from '@/api/passkeys'
import { openInBrowser, passkeysWorkHere } from './passkeyHere'
import { setStepUpHandler, type StepUpRequest } from './stepUp'
import { defaultLabel, webauthnSupported } from './webauthn'

/** A NotAllowedError faster than this is the browser refusing (no user
 *  gesture), not a person dismissing the sheet. */
const GESTURE_REFUSAL_MS = 1500

/**
 * The prompt behind every passkey step-up (canopy-web#1538). Mounted once at
 * the app root; `stepUp.ts`'s middleware calls it when a request comes back
 * `step_up_required`, and retries that request when it resolves. A person with
 * a passkey goes straight to the browser's passkey sheet; this dialog appears
 * only when the browser refuses that (Safari wants a fresh click) or it fails.
 *
 * A person with no passkey yet is offered to register THIS device right here —
 * registering verifies them, which opens the step-up window too, so the
 * original request then goes through without a second prompt.
 *
 * Where no passkey can be used — the Canopy Runner app, whose session is minted
 * from a token (`passkeyHere.ts`) — the dialog says so and opens this same page in
 * the person's browser, to do the action there.
 */
export function StepUpDialog(): JSX.Element {
  const [req, setReq] = useState<StepUpRequest | null>(null)
  const [label, setLabel] = useState(defaultLabel())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [handedOff, setHandedOff] = useState(false)
  const settle = useRef<{ resolve: () => void; reject: (e: Error) => void } | null>(null)

  useEffect(() => {
    const openDialog = (next: StepUpRequest, why = '') =>
      new Promise<void>((resolve, reject) => {
        settle.current = { resolve, reject }
        setError(why)
        setBusy(false)
        setHandedOff(false)
        setReq(next)
      })
    setStepUpHandler(async (next) => {
      if (next.inBrowser || !passkeysWorkHere()) return openDialog({ ...next, inBrowser: true })
      if (!next.hasPasskey || !webauthnSupported()) return openDialog(next)
      // Go straight to the browser's passkey sheet: the dialog is only a fresh
      // click for browsers (Safari) that refuse WebAuthn outside one. Such a
      // refusal is an immediate NotAllowedError; a dismissal takes a person.
      const started = Date.now()
      try {
        await performStepUp()
        return
      } catch (e) {
        const refused = e instanceof Error && e.name === 'NotAllowedError'
        if (refused && Date.now() - started > GESTURE_REFUSAL_MS) throw new Error('Passkey confirmation cancelled')
        return openDialog(next, refused ? '' : e instanceof Error ? e.message : '')
      }
    })
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

  if (req?.inBrowser) {
    const open = () => {
      openInBrowser(window.location.href)
      setHandedOff(true)
    }
    return (
      <Dialog open onOpenChange={(o: boolean) => !o && close(false)}>
        <DialogContent className="w-[calc(100%-2rem)] max-w-md bg-card" data-testid="step-up-dialog">
          <DialogTitle className="text-sm font-semibold text-foreground">Finish this in your browser</DialogTitle>
          <DialogDescription className="text-[13px] text-foreground-secondary">
            This action needs your passkey, and passkeys don’t work inside the Canopy Runner app. Open this page
            in your browser and do it there — your passkey works in your browser.
          </DialogDescription>
          {handedOff && (
            <p className="text-[13px] text-foreground" data-testid="step-up-handed-off">
              Opened in your browser. Finish there; you can close this.
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => close(false)} data-testid="step-up-cancel">
              {handedOff ? 'Close' : 'Cancel'}
            </Button>
            {!handedOff && (
              <Button type="button" size="sm" onClick={open} data-testid="step-up-in-browser">
                Open in your browser
              </Button>
            )}
          </div>
        </DialogContent>
      </Dialog>
    )
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
