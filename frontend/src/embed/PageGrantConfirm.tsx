import { useEffect, useState } from 'react'
import { type PageGrant } from './pageGrant'
import { openInBrowser, PASSKEY_IN_BROWSER, passkeysWorkHere, useWhileAway } from '@/auth/passkeyHere'

/** A confirmed act-as-you permission while it is live, else null. */
export function useConfirmedGrant(pageGrant: PageGrant | undefined) {
  const [grant, setGrant] = useState(() => pageGrant?.current() ?? null)
  useEffect(() => {
    if (!pageGrant) return
    setGrant(pageGrant.current())
    return pageGrant.subscribe(() => setGrant(pageGrant.current()))
  }, [pageGrant])
  return grant?.confirmed ? grant : null
}

/** The passkey prompt for an act-as-you permission the page is waiting on, shown only
 *  once something needs it: the agent's turn tried to act as the person, or a live
 *  card did (`pageGrant.asking`). Nothing the host granted is usable until the
 *  person confirms here, in canopy's frame; "Not now" stops it, and the agent is
 *  told so when it tries again. Its own button, because
 *  Safari runs WebAuthn only inside a click. Where the frame cannot use a passkey
 *  (the Canopy Runner app), or the passkey failed here, the person confirms the same
 *  grant on canopy's page in their browser, and this prompt closes when it sees that
 *  (`passkeyHere.ts`). */
export function PageGrantConfirm({ pageGrant, agentName }: { pageGrant: PageGrant; agentName: string }) {
  const [grant, setGrant] = useState(() => pageGrant.current())
  const [asking, setAsking] = useState(() => pageGrant.asking())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [inBrowser, setInBrowser] = useState(false)
  useEffect(() => pageGrant.subscribe(() => {
    setGrant(pageGrant.current())
    setAsking(pageGrant.asking())
  }), [pageGrant])
  useWhileAway(() => pageGrant.refresh(), inBrowser && asking)
  // Only once something needs it (the turn or a card): a send alone never asks.
  if (!grant || !asking) return null
  const who = grant.agent_name || agentName
  const here = passkeysWorkHere()
  const confirmInBrowser = () => {
    openInBrowser(`/confirm/page-permission/${grant.id}`)
    setError(null)
    setInBrowser(true)
  }
  const confirm = () => {
    setBusy(true)
    setError(null)
    pageGrant.confirm()
      // canopy's own sentence (no passkey yet: where to register one), else the browser's.
      .catch((e: unknown) => setError((e as { detail?: string })?.detail
        || (e instanceof Error && e.name !== 'NotAllowedError' ? e.message : '')
        || 'The passkey was not confirmed.'))
      .finally(() => setBusy(false))
  }
  const decline = () => {
    setBusy(true)
    void pageGrant.stop().finally(() => setBusy(false))
  }
  return (
    <div role="dialog" aria-modal="true" aria-label="Confirm act as you"
         className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4"
         data-testid="page-grant-confirm">
      <div className="w-full max-w-sm space-y-3 rounded-lg border border-border bg-card p-4">
        <p className="text-sm font-medium text-foreground">
          {who} wants to act as you on {grant.site}
        </p>
        {grant.requested_reason && (
          <p className="rounded bg-muted px-2 py-1.5 text-sm text-foreground" data-testid="page-grant-reason">
            To {grant.requested_reason.replace(/^to\s+/i, '')}
          </p>
        )}
        <p className="text-[12px] text-muted-foreground">
          For 30 minutes, it can do what you can do on {grant.site} for this page
          ({grant.scope || 'this page'}), and nothing more. Confirm with your passkey;
          you can stop it at any time.
        </p>
        {inBrowser ? (
          <p className="text-[12px] text-foreground" data-testid="page-grant-in-browser">
            Waiting for you to confirm in your browser…
          </p>
        ) : !here && (
          <p className="text-[12px] text-muted-foreground">{PASSKEY_IN_BROWSER}</p>
        )}
        {error && (
          <p className="text-[12px] text-destructive">
            {error}{' '}
            <button type="button" onClick={confirmInBrowser} className="underline"
                    data-testid="page-grant-try-browser">
              Confirm in your browser instead
            </button>
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={decline} disabled={busy}
                  className="rounded px-2 py-1 text-[12px] text-muted-foreground hover:bg-muted disabled:opacity-50">
            Not now
          </button>
          <button type="button" onClick={here && !inBrowser ? confirm : confirmInBrowser} disabled={busy}
                  className="rounded bg-primary px-3 py-1 text-[12px] text-primary-foreground hover:opacity-90 disabled:opacity-50">
            {inBrowser ? 'Open it again' : here ? 'Confirm with passkey' : 'Confirm in your browser'}
          </button>
        </div>
      </div>
    </div>
  )
}
