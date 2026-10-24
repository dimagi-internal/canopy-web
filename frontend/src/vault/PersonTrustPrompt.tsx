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
 * before is a warning, not a formality: it is what a substituted key looks like.
 * The same rule the ZK vault's secret sealing follows (`trust.ts`).
 */
export function usePersonTrust(): { confirm: ConfirmPerson; element: ReactNode } {
  const [ask, setAsk] = useState<{ email: string; verdict: PersonVerdict } | null>(null)
  const pending = useRef<((ok: boolean) => void) | null>(null)

  const confirm = useCallback<ConfirmPerson>((who) => new Promise<boolean>((resolve) => {
    pending.current?.(false)
    pending.current = resolve
    setAsk(who)
  }), [])

  const finish = (ok: boolean) => {
    const p = pending.current
    pending.current = null
    setAsk(null)
    p?.(ok)
  }

  const changed = ask?.verdict.status === 'changed'
  const element = ask && (
    <div role="dialog" aria-label="Check their vault key" data-testid="person-trust"
         className={`mt-2 space-y-2 rounded-md border p-2 text-[12px] ${changed
           ? 'border-destructive/40 bg-destructive/10' : 'border-warning/40 bg-warning/10'}`}>
      {changed ? (
        <p className="m-0 text-foreground">
          <b>{ask.email}</b>&apos;s vault key changed since this browser last trusted it
          (was <span className="font-mono">{fingerprint(ask.verdict.was ?? '')}</span>). That is what a substituted
          key looks like. Continue only if they told you they replaced it, and the new fingerprint matches theirs:
        </p>
      ) : (
        <p className="m-0 text-foreground">
          This browser has not trusted <b>{ask.email}</b>&apos;s vault key before. Check this fingerprint with them
          (Settings → Vault key shows theirs) before you give them this private conversation:
        </p>
      )}
      <p className="m-0 font-mono text-foreground" data-testid="person-trust-fingerprint">{fingerprint(ask.verdict.jkt)}</p>
      <div className="flex flex-wrap gap-2">
        <button type="button" data-testid="person-trust-ok" onClick={() => finish(true)}
                className="rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground">
          It matches — trust this key
        </button>
        <button type="button" data-testid="person-trust-cancel" onClick={() => finish(false)}
                className="rounded-md border border-input px-3 py-1 text-foreground">
          Cancel
        </button>
      </div>
    </div>
  )
  return { confirm, element }
}
