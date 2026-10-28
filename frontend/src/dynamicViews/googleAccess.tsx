/**
 * A page lending an agent the VIEWER's own Google account for a short session, without
 * leaving the page (`canopy.googleAccess.start/end` in a dynamic view).
 *
 * The frame cannot sign anyone in to Google (its sandbox has no popups, and it must not
 * vouch for itself), so it only asks; canopy shows its own dialog here, in the page,
 * saying in plain words what the agent gets and for how long. On "Continue" canopy opens
 * Google in a small window (a click on canopy's own button, so it is not blocked), the
 * agent's runner finishes the sign-in, and the window closes. A session asks Google for
 * no refresh token: it ends at its time on its own, or when the viewer ends it here.
 * Nothing is left in Settings afterwards. Backend: apps/agents/credentials/google_mint.py.
 */
import { useState } from 'react'
import { getGoogleDelegation, startGoogleDelegation } from '@/api/agents'
import { apiV2 } from '@/api/client.v2'
import { withdrawMyDelegatedCredential } from '@/api/delegatedCredentials'
import type { components } from '@/api/generated'
import { followMint } from '@/pages/agents/googleMint'
import { delegationOutcome, agentName } from '@/pages/agents/ConnectGooglePage'

export type ConnectedAccount = components['schemas']['MyConnectedAccountOut']

export interface GoogleAccessRequest {
  action: 'start' | 'end'
  agent: string
  account?: string
  services?: string[]
  minutes?: number
  /** The page's own words for why it asks, shown as the page's (it is not canopy's). */
  purpose?: string
}

export interface GoogleAccessAsk {
  agent: string
  account: string
  services: string[]
  minutes: number
  purpose: string
}

const SERVICE_WORDS: Record<string, string> = {
  gmail: 'read and organize your Gmail (archive, label, report spam)',
  calendar: 'read and change your Google Calendar',
  drive: 'read and change your Google Drive files',
  docs: 'read and change your Google Docs',
  sheets: 'read and change your Google Sheets',
}

/** What a view may ask for: an agent it declares tasks for, 1-60 minutes, known services. */
export function checkAsk(req: GoogleAccessRequest, declaredAgents: readonly string[]): GoogleAccessAsk | string {
  const agent = String(req.agent || '')
  if (!declaredAgents.includes(agent)) return `this view declares no task for ${agent || 'that agent'}`
  const minutes = Math.round(Number(req.minutes ?? 60))
  if (!(minutes >= 1 && minutes <= 60)) return 'a session lasts 1 to 60 minutes'
  const services = (req.services?.length ? req.services : ['gmail']).map((s) => String(s).toLowerCase())
  if (services.some((s) => !SERVICE_WORDS[s])) return `unknown Google service in ${services.join(', ')}`
  return { agent, account: String(req.account || '').trim(), services, minutes, purpose: String(req.purpose || '').slice(0, 300) }
}

export async function listConnectedAccounts(): Promise<ConnectedAccount[]> {
  const res = await apiV2.GET('/api/me/connected-accounts/')
  if (!res.response.ok || !res.data) throw new Error('could not read your connected accounts')
  return res.data as ConnectedAccount[]
}

/** End the viewer's sessions (and any standing connection) of `account` to `agent`. */
export async function endGoogleAccess(agent: string, account: string): Promise<{ ended: number }> {
  const rows = (await listConnectedAccounts()).filter(
    (r) => r.agent === agent && r.provider === 'google' && (!account || r.account.toLowerCase() === account.toLowerCase()))
  let ended = 0
  for (const r of rows) if (await withdrawMyDelegatedCredential(r.id)) ended += 1
  return { ended }
}

function popupNote(w: Window | null, text: string) {
  try {
    if (w && !w.closed && w.location.href === 'about:blank') {
      w.document.title = 'Google sign-in'
      w.document.body.style.cssText = 'font:16px system-ui;padding:2rem'
      w.document.body.textContent = text
    }
  } catch {
    // Already on Google: nothing to say from here.
  }
}

export function GoogleAccessDialog({ ask, onDone }: {
  ask: GoogleAccessAsk
  onDone: (result: { ok: boolean; account?: string; until?: string | null; outcome?: string; error?: string }) => void
}) {
  const [account, setAccount] = useState(ask.account)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const name = agentName(ask.agent)
  const what = ask.services.map((s) => SERVICE_WORDS[s]).join(', and ')

  const go = async () => {
    setError(null)
    const popup = window.open('about:blank', 'canopy-google-session', 'popup,width=520,height=680')
    if (!popup) {
      setError('Your browser blocked the Google window. Allow pop-ups for canopy, then press Continue again.')
      return
    }
    popupNote(popup, `Preparing the Google sign-in on ${name}'s machine…`)
    setBusy(`Preparing the Google sign-in on ${name}'s machine…`)
    try {
      const mint = await startGoogleDelegation(ask.agent, account.trim(), ask.services, ask.minutes)
      const ready = await followMint(() => getGoogleDelegation(ask.agent, mint.id),
        (m) => Boolean(m.authorize_url) || m.status === 'failed')
      if (!ready?.authorize_url) {
        popup.close()
        throw new Error(ready?.detail || `${name}'s machine did not prepare the sign-in in time. Try again in a minute.`)
      }
      popup.location.href = ready.authorize_url
      setBusy('Finish signing in to Google in the small window.')
      const end = await followMint(
        () => getGoogleDelegation(ask.agent, mint.id),
        (m) => m.status === 'done' || m.status === 'failed' || (popup.closed && m.status === 'awaiting'),
        { intervalMs: 1500, timeoutMs: 10 * 60_000 })
      if (!popup.closed) popup.close()
      if (!end || end.status === 'awaiting') throw new Error('The Google window was closed before you finished signing in.')
      if (end.status !== 'done' || end.outcome !== 'ok') {
        const why = delegationOutcome(end.outcome || 'expired', { slug: name, account: end.account, expected: end.expected_account })
        throw new Error(why?.text || end.detail || 'The sign-in did not finish.')
      }
      const row = (await listConnectedAccounts()).find(
        (r) => r.agent === ask.agent && r.account.toLowerCase() === account.trim().toLowerCase())
      onDone({ ok: true, account: end.account || account.trim(), until: row?.session_until ?? null, outcome: 'ok' })
    } catch (e) {
      setBusy(null)
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" role="dialog" aria-modal="true"
      aria-labelledby="google-access-title" data-testid="google-access-dialog">
      <div className="w-full max-w-md space-y-3 rounded-xl border border-border bg-card p-5 text-sm text-foreground shadow-xl">
        <h2 id="google-access-title" className="text-base font-semibold">
          Let {name} use your Google account for {ask.minutes} minutes
        </h2>
        {ask.purpose && <p className="text-muted-foreground">This page says: “{ask.purpose}”</p>}
        <ul className="list-disc space-y-1 pl-5">
          <li>You sign in to Google yourself, in a small window.</li>
          <li>For the next <b>{ask.minutes} minutes</b>, {name} can {what} — only for what you ask it on this page.</li>
          <li>Then it ends by itself: Google gives {name} no long-lived key. You can also end it sooner from the page.</li>
          <li>Nothing is kept in your settings, and no one can read your sign-in in canopy, you included.</li>
        </ul>
        {ask.services.includes('gmail') && (
          <p className="text-xs text-muted-foreground">
            Google’s Gmail permission also covers sending email; whether {name} ever sends is up to its own rules.
          </p>
        )}
        <label className="block">
          Your Google account
          <input type="email" value={account} onChange={(e) => setAccount(e.target.value)} placeholder="you@gmail.com"
            disabled={!!busy}
            className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1 text-foreground" />
        </label>
        {busy && <p className="text-muted-foreground" data-testid="google-access-progress">{busy}</p>}
        {error && <p className="text-destructive" data-testid="google-access-error">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="rounded-md border border-border px-3 py-1.5 hover:bg-muted"
            onClick={() => onDone({ ok: false, error: 'cancelled' })}>Cancel</button>
          <button type="button" disabled={!!busy || !account.trim()} onClick={go}
            className="rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-50">
            Continue to Google
          </button>
        </div>
      </div>
    </div>
  )
}
