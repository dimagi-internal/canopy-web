import { useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { confirmMyHostGrant, getMyHostGrant, stopMyHostGrant, type HostGrant } from '@/api/hostGrants'

/**
 * Confirm a page's act-as-you permission in your browser
 * (`/confirm/page-permission/:grantId`). A page's panel asks for it with a passkey
 * in its own frame; where that frame cannot use one — the Canopy Runner app, see
 * `auth/passkeyHere.ts` — the panel opens this page in the browser instead. It is
 * the same waiting permission: confirming it here confirms it for the panel, which
 * closes its prompt as soon as it next reads it. Not now stops it, as the panel's does.
 */
export function PagePermissionConfirmPage(): JSX.Element | null {
  const { grantId } = useParams()
  const id = Number(grantId)
  const [grant, setGrant] = useState<HostGrant | null>(null)
  const [state, setState] = useState<'loading' | 'open' | 'stopped' | 'gone'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!Number.isInteger(id)) return
    let cancelled = false
    getMyHostGrant(id)
      .then((g) => {
        if (cancelled) return
        setGrant(g)
        setState('open')
      })
      .catch(() => !cancelled && setState('gone'))
    return () => {
      cancelled = true
    }
  }, [id])

  if (!Number.isInteger(id)) return null
  if (state === 'loading') return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />
  if (state === 'gone' || !grant) {
    return (
      <p className="max-w-2xl text-sm text-foreground-secondary" data-testid="page-permission-gone">
        This permission isn’t waiting any more: it was stopped, or it ran out. Send from the page again to ask
        for a new one.
      </p>
    )
  }

  const confirm = async () => {
    setBusy(true)
    setError(null)
    try {
      setGrant(await confirmMyHostGrant(id))
    } catch (e) {
      const detail = (e as { detail?: string })?.detail
      setError(detail || (e instanceof Error && e.name !== 'NotAllowedError' ? e.message : 'The passkey was not confirmed.'))
    } finally {
      setBusy(false)
    }
  }
  const decline = async () => {
    setBusy(true)
    await stopMyHostGrant(id)
    setBusy(false)
    setState('stopped')
  }

  const who = grant.agent_name || 'The agent'
  const until = new Date(grant.expires_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return (
    <div className="max-w-2xl space-y-4">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The permission">
        <h1 className="text-sm font-semibold text-foreground">
          Let {who} act as you on {grant.site} for 30 minutes?
        </h1>
        <p className="mt-2 text-[13px] text-foreground-secondary">
          It can do what you can do on {grant.site} for that page ({grant.scope || 'that page'}), and nothing
          more. You can stop it at any time.
        </p>
        {state === 'stopped' ? (
          <p className="mt-3 text-[13px] text-foreground" data-testid="page-permission-stopped">
            Stopped. {who} won’t act as you there; you can close this tab.
          </p>
        ) : grant.confirmed ? (
          <p className="mt-3 text-[13px] text-foreground" data-testid="page-permission-confirmed">
            Confirmed until {until}. The page you came from has it now — you can close this tab.
          </p>
        ) : (
          <>
            {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
            <div className="mt-4 flex flex-wrap gap-2">
              <Button disabled={busy} onClick={() => void confirm()} data-testid="page-permission-confirm">
                {busy ? 'Waiting for your passkey…' : 'Confirm with passkey'}
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => void decline()}>
                Not now
              </Button>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
