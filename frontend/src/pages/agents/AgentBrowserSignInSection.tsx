import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'

import {
  getBrowserSignIn,
  switchOffBrowserSignIn,
  switchOnBrowserSignIn,
  type BrowserSignInOut,
} from '@/api/browserSignIn'
import { LoadFailed } from '@/components/vault/LoadFailed'
import { relativeAge } from '@/lib/relativeAge'

// Browser sign-in (apps/agents/browser_sign_in.py): off by default; while on, the agent's
// own Google account may sign in to canopy-web at a browser — e.g. the browser extension's
// side panel — as the agent's login, with its normal permissions. Until switched off, or
// for a set number of days.

// '' = until switched off.
const DAY_CHOICES = ['', '1', '7', '30'] as const

export function AgentBrowserSignInSection({ slug, canEdit }: { slug: string; canEdit: boolean }) {
  const [state, setState] = useState<BrowserSignInOut | null>(null)
  const [days, setDays] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getBrowserSignIn(slug).then(
      (s) => !cancelled && setState(s),
      () => !cancelled && setFailed(true),
    )
    return () => {
      cancelled = true
    }
  }, [slug, canEdit])

  if (!canEdit) return null
  if (state === null) return failed ? <LoadFailed what="browser sign-in" /> : null

  const act = async (fn: () => Promise<BrowserSignInOut>) => {
    setBusy(true)
    setError(null)
    try {
      setState(await fn())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const who = state.login_email || 'its canopy login'
  return (
    <section id="browser-sign-in" className="mb-6" data-testid="agent-browser-sign-in">
      <p className="text-[12px] text-muted-foreground">
        Lets {who} sign in to Canopy in a browser (e.g. to use or test the browser extension as this agent), with the
        agent&rsquo;s own permissions. Switching it off signs those sessions out.
      </p>
      <p className="mt-2 text-[12px] text-foreground" data-testid="browser-sign-in-summary">
        {!state.login_email
          ? 'This agent has no canopy login of its own.'
          : state.enabled
            ? `On · switched on by ${state.granted_by_email || 'someone'} ${relativeAge(state.granted_at ?? null, new Date(), { seconds: false })}` +
              (state.expires_at
                ? ` · ends ${relativeAge(state.expires_at, new Date(), { future: true })}`
                : ' · until switched off')
            : 'Off'}
      </p>
      {state.login_email && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {state.enabled ? (
            <Button size="sm" variant="outline" disabled={busy}
              onClick={() => void act(() => switchOffBrowserSignIn(slug))} data-testid="browser-sign-in-off">
              Switch off
            </Button>
          ) : (
            <>
              <select aria-label="For how long" value={days} disabled={busy} onChange={(e) => setDays(e.target.value)}
                className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground"
                data-testid="browser-sign-in-days">
                {DAY_CHOICES.map((d) => (
                  <option key={d} value={d}>{d === '' ? 'Until switched off' : d === '1' ? 'For 1 day' : `For ${d} days`}</option>
                ))}
              </select>
              <Button size="sm" disabled={busy}
                onClick={() => void act(() => switchOnBrowserSignIn(slug, days ? Number(days) : null))} data-testid="browser-sign-in-on">
                Switch on
              </Button>
            </>
          )}
        </div>
      )}
      {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
    </section>
  )
}
