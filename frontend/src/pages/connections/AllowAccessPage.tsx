import { useEffect, useState, type JSX } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getCsrfToken } from '@/api/base'
import { connectionStartAction, listMyConnections } from '@/api/connections'
import { safeReturnTo } from './ShareConnectionPage'

/** The lengths offered first. `minutes` is what the start receives (apps/connections/views.py). */
export const GRANT_CHOICES: readonly { label: string; minutes: number }[] = [
  { label: '1 hour', minutes: 60 },
  { label: '8 hours', minutes: 8 * 60 },
  { label: '1 day', minutes: 24 * 60 },
  { label: '7 days', minutes: 7 * 24 * 60 },
]
/** The server's limits (`apps/connections/temporary.py::SHORTEST_GRANT`, `LONGEST_GRANT`), which it checks again. */
export const SHORTEST_GRANT_MINUTES = 5
export const LONGEST_GRANT_MINUTES = 30 * 24 * 60

type Unit = 'hours' | 'days'

/** A custom length in minutes, or an error sentence. */
export function customMinutes(amount: string, unit: Unit): number | string {
  const n = Number(amount)
  if (!amount.trim() || !Number.isFinite(n) || n <= 0) return 'Enter how many hours or days.'
  const minutes = Math.round(n * (unit === 'days' ? 24 * 60 : 60))
  if (minutes < SHORTEST_GRANT_MINUTES) return 'Choose at least a few minutes.'
  if (minutes > LONGEST_GRANT_MINUTES) return 'The longest you can allow is 30 days.'
  return minutes
}

/** "Tuesday, 3 December, 14:30": when a grant of `minutes` from `now` ends, in the viewer's own time. */
export function endsAt(minutes: number, now: Date = new Date()): string {
  return new Date(now.getTime() + minutes * 60_000).toLocaleString(undefined, {
    weekday: 'long', day: 'numeric', month: 'long', hour: 'numeric', minute: '2-digit',
  })
}

function titleOf(provider: string): string {
  return provider ? provider[0].toUpperCase() + provider.slice(1) : 'the account'
}

/**
 * "Allow Salesforce for a while" (`/w/:workspace/connections/:provider/allow`): where a person allows
 * their own account for their screens and apps in this workspace, for a length they choose. canopy's
 * start of such a grant sends any link here (apps/connections/views.py), and only this page's form, a
 * CSRF-checked POST, starts one; the provider's own sign-in comes next and lands back on `return_to`.
 */
export function AllowAccessPage(): JSX.Element {
  const { workspace = '', provider = '' } = useParams()
  const [params] = useSearchParams()
  const returnTo = safeReturnTo(params.get('return_to'))
  const [title, setTitle] = useState(titleOf(provider))
  const [choice, setChoice] = useState<number | 'custom'>(GRANT_CHOICES[0].minutes)
  const [amount, setAmount] = useState('2')
  const [unit, setUnit] = useState<Unit>('days')

  useEffect(() => {
    let cancelled = false
    listMyConnections()
      .then((v) => {
        const known = v.providers.find((p) => p.name === provider)
        if (!cancelled && known) setTitle(known.title)
      })
      .catch(() => { /* the name from the address will do */ })
    return () => { cancelled = true }
  }, [provider])

  const custom = choice === 'custom' ? customMinutes(amount, unit) : null
  const minutes = choice === 'custom' ? (typeof custom === 'number' ? custom : null) : choice
  const radio = 'h-4 w-4 accent-primary'

  return (
    <div className="max-w-xl space-y-4" data-testid="allow-access">
      <h1 className="text-lg font-semibold text-foreground">Allow {title} for a while</h1>
      <p className="text-sm text-foreground">
        Pages and apps in this workspace that read {title} will use your own {title} account until the time you
        choose. Next, {title} asks you to sign in.
      </p>
      <form method="post" action={connectionStartAction(provider, workspace)} className="space-y-4">
        <input type="hidden" name="csrfmiddlewaretoken" value={getCsrfToken()} />
        <input type="hidden" name="temporary" value="1" />
        <input type="hidden" name="for_screens" value="1" />
        <input type="hidden" name="return_to" value={returnTo} />
        {minutes !== null && <input type="hidden" name="minutes" value={String(minutes)} />}
        <fieldset className="space-y-2 rounded-lg border border-border bg-card p-4">
          <legend className="px-1 text-sm font-medium text-foreground">For how long?</legend>
          {GRANT_CHOICES.map((c) => (
            <label key={c.minutes} className="flex items-center gap-2 text-sm text-foreground">
              <input type="radio" name="length" className={radio} checked={choice === c.minutes}
                onChange={() => setChoice(c.minutes)} />
              {c.label}
            </label>
          ))}
          <label className="flex flex-wrap items-center gap-2 text-sm text-foreground">
            <input type="radio" name="length" className={radio} checked={choice === 'custom'}
              onChange={() => setChoice('custom')} />
            Another length:
            <input type="number" min="1" step="1" inputMode="numeric" aria-label="How many"
              className="w-20 rounded border border-input bg-background px-2 py-1 text-sm"
              value={amount} onChange={(e) => { setAmount(e.target.value); setChoice('custom') }} />
            <select aria-label="Hours or days" className="rounded border border-input bg-background px-2 py-1 text-sm"
              value={unit} onChange={(e) => { setUnit(e.target.value as Unit); setChoice('custom') }}>
              <option value="hours">hours</option>
              <option value="days">days</option>
            </select>
          </label>
          {typeof custom === 'string' && <p role="alert" className="text-[13px] text-destructive">{custom}</p>}
          {minutes !== null && <p className="text-[13px] text-muted-foreground">Ends {endsAt(minutes)}.</p>}
        </fieldset>
        <p className="text-[13px] text-muted-foreground">
          Until then canopy holds your {title} sign-in and can use it; it uses it only for the pages and apps
          you open here. When the time is up, canopy ends it at {title} and deletes it. You can end it sooner
          in Settings → Connections.
        </p>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <button type="submit" disabled={minutes === null}
            className="inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            Continue to {title}
          </button>
          <Link to={returnTo} className="text-sm text-primary hover:underline">Cancel</Link>
        </div>
      </form>
    </div>
  )
}
