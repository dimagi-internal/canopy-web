import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { getGoogleDelegation, startGoogleDelegation, type GoogleMintOut } from '@/api/agents'
import { followMint, mintOutcome, type MintOutcome } from '@/pages/agents/googleMint'

// A person connects THEIR OWN Google account to an agent, as a credential they
// delegate to it (`google-delegated-<account>`, apps/agents/credentials/google_mint.py).
// The sign-in finishes on one of the agent's runners, which seals the token to the
// machines that may hold a credential of theirs: canopy never holds it, and nobody can
// read it — this person included. A page links here with `?services=gmail` (only
// those services are offered) and may pass `?return=<canopy path>` to be sent back to
// once the account is connected.

export const DELEGATED_SERVICES = ['gmail', 'calendar', 'drive', 'docs', 'sheets'] as const
const LABEL: Record<string, string> = {
  gmail: 'Gmail',
  calendar: 'Calendar',
  drive: 'Drive',
  docs: 'Docs',
  sheets: 'Sheets',
}

/** What each service lets the agent do, said plainly under its box. */
const WHAT: Record<string, string> = {
  gmail: 'Read, label, archive and report spam. Google’s Gmail permission also allows sending; whether the agent ever sends is up to its own rules.',
}

const RETURN_KEY = 'canopy.connect-google.return'

/** A canopy path to go back to after connecting — never another site. */
export function safeReturn(path: string | null | undefined): string | null {
  if (!path || !path.startsWith('/') || path.startsWith('//') || path.startsWith('/api/')) return null
  return path
}

function rememberReturn(slug: string, path: string | null) {
  try {
    if (path) sessionStorage.setItem(`${RETURN_KEY}:${slug}`, path)
  } catch {
    // Storage blocked: the person just won't get the "back" link.
  }
}

function recallReturn(slug: string): string | null {
  try {
    return safeReturn(sessionStorage.getItem(`${RETURN_KEY}:${slug}`))
  } catch {
    return null
  }
}

/** The agent's name as people say it: `bailey` → `Bailey`. */
export function agentName(slug: string): string {
  return slug ? slug.charAt(0).toUpperCase() + slug.slice(1) : slug
}

export function servicesFrom(param: string | null): string[] {
  const wanted = (param || 'gmail').split(',').map((s) => s.trim().toLowerCase())
  const known = DELEGATED_SERVICES.filter((s) => wanted.includes(s))
  return known.length ? [...known] : ['gmail']
}

/** How a delegated sign-in ended, in the person's terms. */
export function delegationOutcome(
  status: string | null,
  ctx: { account?: string | null; expected?: string | null; slug: string },
): MintOutcome | null {
  switch (status) {
    case 'ok':
      return {
        tone: 'ok',
        text: `Connected. ${ctx.slug} can now use ${ctx.account || 'your account'} when you ask it to. Disconnect any time from your settings.`,
        link: { href: '/settings', label: 'Your delegated credentials' },
      }
    case 'pending':
      return { tone: 'ok', text: `Google answered — ${ctx.slug}’s runner is finishing the sign-in…` }
    case 'not-admin':
      return { tone: 'error', text: `You are no longer an admin of ${ctx.slug}, so nothing was stored.` }
    case 'taken':
      return { tone: 'error', text: `${ctx.slug} already holds that account from someone else. Nothing was stored.` }
    case 'outdated-runner':
      return { tone: 'error', text: 'The runner’s canopy CLI cannot seal a delegated Google account yet. It updates itself — try again later.' }
    case 'zk-no-recipients':
      return { tone: 'error', text: `No machine that runs ${ctx.slug} could open it, so nothing was stored.` }
    case 'wrong-account':
      return {
        tone: 'error',
        text: `Google signed in as ${ctx.account || 'a different account'}, not ${ctx.expected || 'the account you named'}. Nothing was stored — sign in as that account and try again.`,
      }
    default:
      return mintOutcome(status, { account: ctx.account, agentEmail: ctx.expected })
  }
}

export function ConnectGooglePage() {
  const { slug = '' } = useParams()
  const [params] = useSearchParams()
  const [account, setAccount] = useState(params.get('account') ?? '')
  const [services, setServices] = useState<string[]>(() => servicesFrom(params.get('services')))
  // Asked for specific services (`?services=gmail`): offer only those, not every Google app.
  const [offered] = useState<readonly string[]>(() => (params.get('services') ? servicesFrom(params.get('services')) : DELEGATED_SERVICES))
  const name = agentName(slug)
  // Where to send them back once connected: `?return=` on the way in, kept across Google's round trip.
  const [backTo] = useState<string | null>(() => {
    const given = safeReturn(params.get('return'))
    if (given) rememberReturn(slug, given)
    return given ?? recallReturn(slug)
  })
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [finished, setFinished] = useState<GoogleMintOut | null>(null)
  const returnedMint = params.get('mint')

  // Back from Google (`?google=pending&mint=…`): the runner finishes after this, so follow it.
  useEffect(() => {
    if (!returnedMint || params.get('google') !== 'pending') return
    let live = true
    void (async () => {
      try {
        const end = await followMint(
          () => getGoogleDelegation(slug, returnedMint),
          (m) => m.status === 'done' || m.status === 'failed',
        )
        if (!live) return
        if (end) setFinished(end)
        else setError('The runner has not finished the sign-in yet — reload this page in a minute to see how it ended.')
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : 'Could not read how the sign-in went')
      }
    })()
    return () => {
      live = false
    }
  }, [slug, returnedMint, params])

  const status = finished ? finished.outcome || (finished.status === 'done' ? 'ok' : 'expired') : params.get('google')
  const outcome = delegationOutcome(status, {
    slug: name,
    account: finished?.account ?? params.get('account'),
    expected: finished?.expected_account,
  })

  const toggle = (s: string) => setServices((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]))

  const connect = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const mint = await startGoogleDelegation(slug, account.trim(), services)
      setProgress(`Asking ${mint.runner || `${slug}’s runner`} to prepare the Google sign-in…`)
      const ready = await followMint(
        () => getGoogleDelegation(slug, mint.id),
        (m) => Boolean(m.authorize_url) || m.status === 'failed',
      )
      if (!ready || !ready.authorize_url) {
        setProgress(null)
        setError(ready?.detail || `${mint.runner || 'The runner'} did not prepare the sign-in in time — it may be busy or offline. Try again.`)
        setBusy(false)
        return
      }
      // A TOP-LEVEL navigation: only leaving the page can render Google's consent screen.
      window.location.assign(ready.authorize_url)
    } catch (err) {
      setProgress(null)
      setError(err instanceof Error ? err.message : 'Could not start the Google sign-in')
      setBusy(false)
    }
  }

  return (
    <div className="max-w-xl space-y-4 p-4" data-testid="connect-google">
      <div>
        <h1 className="text-base font-semibold text-foreground">Connect your Google account to {name}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {name} uses it only for work you ask it to do. Nobody can read your sign-in, you included, and you can
          disconnect any time in <Link to="/settings" className="text-primary hover:underline">your settings</Link>.
        </p>
        <details className="mt-1 text-xs text-muted-foreground">
          <summary className="cursor-pointer">How is it kept safe?</summary>
          <p className="mt-1">
            You sign in to Google yourself. The sign-in finishes on a machine that runs {name}, which locks it so that
            only the machines that run {name} can open it. Canopy never holds it.
          </p>
        </details>
      </div>
      {outcome && (
        <p
          className={`text-sm ${outcome.tone === 'ok' ? 'text-success' : 'text-destructive'}`}
          data-testid="connect-google-outcome"
        >
          {outcome.text}
          {outcome.link && (
            <>
              {' '}
              <Link to={outcome.link.href} className="font-medium text-primary hover:underline">{outcome.link.label}</Link>
            </>
          )}
          {outcome.tone === 'ok' && status === 'ok' && backTo && (
            <span className="mt-2 block">
              <Link to={backTo} className="inline-block rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground" data-testid="connect-google-back">
                Go back to where you were
              </Link>
            </span>
          )}
        </p>
      )}
      <form onSubmit={connect} className="space-y-3 rounded-xl border border-border bg-card p-4">
        <label className="block text-sm text-foreground">
          Your Google account
          <input
            type="email"
            required
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            placeholder="you@gmail.com"
            className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground"
          />
        </label>
        <fieldset className="text-sm text-foreground">
          <legend className="mb-1">What {name} may use</legend>
          {offered.map((s) => (
            <div key={s} className="mr-4 inline-block">
              <label className="inline-flex items-center gap-1 text-muted-foreground">
                <input type="checkbox" checked={services.includes(s)} onChange={() => toggle(s)} />
                {LABEL[s]}
              </label>
              {WHAT[s] && offered.length === 1 && <p className="mt-0.5 text-xs text-muted-foreground">{WHAT[s]}</p>}
            </div>
          ))}
        </fieldset>
        <button
          type="submit"
          disabled={busy || !account.trim() || services.length === 0}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {busy ? 'Connecting…' : 'Connect with Google'}
        </button>
        {progress && !error && <p className="text-xs text-muted-foreground">{progress}</p>}
        {error && <p className="text-sm text-destructive" data-testid="connect-google-error">{error}</p>}
      </form>
    </div>
  )
}
