import { useCallback, useRef, useState, type ReactNode } from 'react'
import { fingerprint } from './zk'
import type { PersonVerdict } from './trust'

/** Asked before a private conversation's key is wrapped to someone this browser has not
 *  trusted yet (`trust.trustPerson`): resolves true only when the person confirms. */
export type ConfirmPerson = (who: { email: string; verdict: PersonVerdict }) => Promise<boolean>

/** The default: never wrap to an unconfirmed key; say why, with the fingerprint. */
export const refuseUnconfirmed: ConfirmPerson = async ({ email, verdict }) => {
  throw new Error(verdict.status === 'changed'
    ? `${email}'s vault key changed since this browser last trusted it (was ${fingerprint(verdict.was ?? '')}, `
      + `now ${fingerprint(verdict.jkt)}). Confirm with them before giving them this conversation — nothing was shared.`
    : `This browser has not trusted ${email}'s vault key yet (${fingerprint(verdict.jkt)}). `
      + 'Check the fingerprint with them first — nothing was shared.')
}

/**
 * "Is this their key?" — shown before a private conversation's key is wrapped to
 * someone new to this browser: their fingerprint, to check with them (Settings → Vault
 * key shows theirs), and an explicit yes. A key that CHANGED under a person trusted
 * before is a STOP, not a click-through: it is what a substituted key looks like, so the
 * add goes on only once the adder has typed the new fingerprint as the person read it
 * out to them (`fingerprintMatches`); until then there is no button that continues.
 * The same rule the ZK vault's secret sealing follows (`trust.ts`).
 */
/** Whether `typed` is the fingerprint of `thumbprint` (spaces and case aside). */
export function fingerprintMatches(typed: string, thumbprint: string): boolean {
  const norm = (v: string) => v.replace(/[^A-Za-z0-9]/g, '').toUpperCase()
  const want = norm(fingerprint(thumbprint))
  return want.length > 0 && norm(typed) === want
}

export function usePersonTrust(): { confirm: ConfirmPerson; element: ReactNode } {
  const [ask, setAsk] = useState<{ email: string; verdict: PersonVerdict } | null>(null)
  const [typed, setTyped] = useState('')
  const pending = useRef<((ok: boolean) => void) | null>(null)

  const confirm = useCallback<ConfirmPerson>((who) => new Promise<boolean>((resolve) => {
    pending.current?.(false)
    pending.current = resolve
    setTyped('')
    setAsk(who)
  }), [])

  const finish = (ok: boolean) => {
    const p = pending.current
    pending.current = null
    setAsk(null)
    setTyped('')
    p?.(ok)
  }

  const changed = ask?.verdict.status === 'changed'
  // A changed key continues only on a fingerprint the adder typed and that matches.
  const verified = !changed || (ask ? fingerprintMatches(typed, ask.verdict.jkt) : false)
  const element = ask && (
    <div role="dialog" aria-label="Check their vault key" data-testid="person-trust"
         className={`mt-2 space-y-2 rounded-md border p-2 text-[12px] ${changed
           ? 'border-destructive/40 bg-destructive/10' : 'border-warning/40 bg-warning/10'}`}>
      {changed ? (
        <>
          <p className="m-0 font-medium text-destructive">Stopped: nothing was shared.</p>
          <p className="m-0 text-foreground">
            <b>{ask.email}</b>&apos;s vault key changed since this browser last trusted it
            (was <span className="font-mono">{fingerprint(ask.verdict.was ?? '')}</span>). That is what a substituted
            key looks like. Ask them, by a channel other than canopy, whether they replaced it, and to read you the
            fingerprint Settings → Vault key shows them. Type it here to go on:
          </p>
          <input
            type="text" autoComplete="off" spellCheck={false} value={typed}
            onChange={(e) => setTyped(e.target.value)}
            aria-label="Their new fingerprint, as they read it to you"
            data-testid="person-trust-typed"
            placeholder="XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX"
            className="w-full rounded-md border border-input bg-card px-2 py-1 font-mono text-foreground"
          />
          {typed && !verified ? (
            <p className="m-0 text-destructive" data-testid="person-trust-mismatch">
              That is not the fingerprint canopy named for them. Do not continue.
            </p>
          ) : null}
        </>
      ) : (
        <>
          <p className="m-0 text-foreground">
            This browser has not trusted <b>{ask.email}</b>&apos;s vault key before. Check this fingerprint with them
            (Settings → Vault key shows theirs) before you give them this private conversation:
          </p>
          <p className="m-0 font-mono text-foreground" data-testid="person-trust-fingerprint">{fingerprint(ask.verdict.jkt)}</p>
        </>
      )}
      <div className="flex flex-wrap gap-2">
        {verified ? (
          <button type="button" data-testid="person-trust-ok" onClick={() => finish(true)}
                  className="rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground">
            {changed ? 'They read me this fingerprint — trust their new key' : 'It matches — trust this key'}
          </button>
        ) : null}
        <button type="button" data-testid="person-trust-cancel" onClick={() => finish(false)}
                className="rounded-md border border-input px-3 py-1 text-foreground">
          Cancel
        </button>
      </div>
    </div>
  )
  return { confirm, element }
}
