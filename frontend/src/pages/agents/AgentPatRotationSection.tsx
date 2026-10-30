import { useEffect, useState } from 'react'
import { getPatRotation, rotatePatNow, setPatRotation, type PatRotationOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'

// Automatic rotation of the agent's own canopy login (Canopy Vault, canopy-web#1549).
// canopy-web issued that token, so it can replace it on a schedule without anyone
// typing anything: a new token goes into `CANOPY_WEB_PAT`, runners pick it up at their
// next `cred sync`, and the old one keeps working for 24 h before it is revoked.

export function AgentPatRotationSection({ slug, canEdit }: { slug: string; canEdit: boolean }) {
  const [state, setState] = useState<PatRotationOut | null>(null)
  const [days, setDays] = useState(30)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getPatRotation(slug)
      .then((s) => {
        if (cancelled) return
        setState(s)
        setDays(s.period_days)
      })
      .catch(() => !cancelled && setState(null))
    return () => {
      cancelled = true
    }
  }, [slug, canEdit])

  if (!canEdit || state === null) return null

  const run = async (fn: () => Promise<PatRotationOut>) => {
    setBusy(true)
    setError(null)
    try {
      const next = await fn()
      setState(next)
      setDays(next.period_days)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const validDays = Number.isInteger(days) && days >= 1 && days <= 365
  const last = state.history[0]
  return (
    <section id="cred-pat-rotation" className="mb-6" data-testid="agent-pat-rotation">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Renewal
      </h4>
      <div className="flex flex-wrap items-center gap-2 text-[12px] text-foreground">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={state.enabled}
            disabled={busy || !validDays}
            onChange={() => void run(() => setPatRotation(slug, !state.enabled, days))}
            aria-label="Rotate this agent's canopy login automatically"
            data-testid="pat-rotation-enabled"
          />
          Rotate automatically every
        </label>
        <input
          type="number"
          min={1}
          max={365}
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          onBlur={() => {
            if (state.enabled && validDays && days !== state.period_days) {
              void run(() => setPatRotation(slug, true, days))
            }
          }}
          aria-label="Rotation period in days"
          data-testid="pat-rotation-days"
          className="w-16 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground"
        />
        <span>days</span>
        <button
          type="button"
          onClick={() => {
            if (window.confirm('Rotate its canopy login now? The old token keeps working for 24 hours.')) {
              void run(() => rotatePatNow(slug))
            }
          }}
          disabled={busy}
          title="Mints a new token now — needs your passkey"
          data-testid="pat-rotate-now"
          className="ml-auto min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0"
        >
          Rotate now
        </button>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground" data-testid="pat-rotation-summary">
        {state.last_rotated_at ? `Last rotated ${relativeAge(state.last_rotated_at)}` : 'Never rotated'}
        {state.enabled && state.next_due_at ? ` · next due ${new Date(state.next_due_at).toLocaleDateString()}` : ''}
        {state.enabled && state.enabled_by_email ? ` · on by ${state.enabled_by_email}` : ''}
        {last?.status === 'failed' ? (
          <span className="text-destructive"> · last attempt failed: {last.detail}</span>
        ) : null}
      </p>
      {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
    </section>
  )
}
