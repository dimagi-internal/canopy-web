import { useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { ApiError } from '@/api/problem'
import { decideSignOff, getSignOff, type SignOffOut } from '@/api/signOffs'

const STATE_TEXT: Record<string, string> = {
  approved: 'Approved.',
  declined: 'Declined.',
  expired: 'This request has expired. Ask again from the site.',
}

/**
 * A connected site asking you to approve one action with your passkey
 * (`/w/:workspace/sign-offs/:signOffId`, the `approve_url` the site sends you to).
 *
 * Shows which site asks, the action in its own words, and the account it is for (for
 * an agent's request, which agent). Approve asks for your passkey (the API client's
 * step-up) and sends your browser back to the site with the signed approval; Decline
 * sends it back without one. Only the person it is for, or an agent's owner or admins,
 * can open it.
 */
export function SignOffPage(): JSX.Element | null {
  const { signOffId } = useParams()
  const [row, setRow] = useState<SignOffOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'approve' | 'decline' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [leaving, setLeaving] = useState(false)

  useEffect(() => {
    if (!signOffId) return
    let cancelled = false
    getSignOff(signOffId)
      .then((r) => {
        if (!cancelled) setRow(r)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setLoadError(
          e instanceof ApiError && e.status === 404
            ? 'No such request, or it is not yours to decide.'
            : e instanceof Error ? e.message : 'Failed to load the request',
        )
      })
    return () => {
      cancelled = true
    }
  }, [signOffId])

  if (!signOffId) return null
  if (loadError) {
    return (
      <div className="max-w-2xl">
        <p className="text-sm text-destructive">{loadError}</p>
      </div>
    )
  }
  if (!row) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  async function decide(approve: boolean) {
    if (!row) return
    setBusy(approve ? 'approve' : 'decline')
    setActionError(null)
    try {
      const decided = await decideSignOff(row.id, approve)
      setRow(decided)
      setLeaving(true)
      window.location.assign(decided.redirect_url)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(null)
    }
  }

  const open = row.state === 'open'
  return (
    <div className="max-w-2xl space-y-4">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The request">
        <h1 className="text-sm font-semibold text-foreground">{row.site} asks for your approval</h1>
        <p className="mt-2 text-[14px] text-foreground" data-testid="sign-off-title">
          {row.action_title}
        </p>
        {row.action_detail && (
          <p className="mt-1 whitespace-pre-wrap text-[13px] text-foreground-secondary">{row.action_detail}</p>
        )}
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[8rem_1fr]">
          <dt className="font-medium text-foreground">For</dt>
          <dd className="m-0" data-testid="sign-off-for">
            {row.agent_slug
              ? <>{row.agent_name || row.agent_slug}, an agent you own or administer ({row.subject})</>
              : row.subject}
          </dd>
          <dt className="font-medium text-foreground">Site</dt>
          <dd className="m-0 font-mono">{row.site}</dd>
          <dt className="font-medium text-foreground">Asked</dt>
          <dd className="m-0">{new Date(row.created_at).toLocaleString()}</dd>
          <dt className="font-medium text-foreground">Expires</dt>
          <dd className="m-0">{new Date(row.expires_at).toLocaleString()}</dd>
        </dl>
      </section>

      {actionError && <p className="text-sm text-destructive" role="alert">{actionError}</p>}

      {open ? (
        <section className="rounded-lg border border-border bg-card p-5" aria-label="Decide">
          <p className="text-[13px] text-foreground-secondary">
            Approving asks for your passkey, then takes you back to {row.site}. Decline unless you started this.
          </p>
          <div className="mt-3 flex gap-2">
            <Button type="button" size="sm" disabled={busy !== null || leaving} onClick={() => void decide(true)}>
              {busy === 'approve' ? 'Approving…' : 'Approve'}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy !== null || leaving}
              onClick={() => void decide(false)}
            >
              {busy === 'decline' ? 'Declining…' : 'Decline'}
            </Button>
          </div>
        </section>
      ) : (
        <p className="text-[13px] text-foreground-secondary" data-testid="sign-off-state">
          {leaving ? `Taking you back to ${row.site}…` : STATE_TEXT[row.state] ?? row.state}
          {!leaving && row.decided_by_email && ` (${row.decided_by_email})`}
        </p>
      )}
    </div>
  )
}
