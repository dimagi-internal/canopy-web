import { useEffect, useState } from 'react'

import { askMySessionReview, getMySessionReview, withdrawMySessionReview } from '@/api/people'
import type { SessionReviewOffer } from '@/api/people'
import { scopedPath } from '@/lib/scopedLinks'

/** The coach this session could be asked to review by, or null. Asked only of a session the
 *  viewer owns; a failure hides the menu item, since the offer is never what the page is for. */
export function useSessionReviewOffer(sessionId: string, enabled: boolean) {
  const [offer, setOffer] = useState<SessionReviewOffer | null>(null)
  useEffect(() => {
    if (!enabled) return
    let live = true
    getMySessionReview(sessionId)
      .then((s) => { if (live) setOffer(s.offer ?? null) })
      .catch(() => { if (live) setOffer(null) })
    return () => { live = false }
  }, [sessionId, enabled])
  return [offer, setOffer] as const
}

/** "Ask <coach> to review this": what it does, then the press; or, once asked, where the reply
 *  is and how to take it back. Each sentence is a claim about `access._review_ask_q` and
 *  `apps/contacts/session_review_asks.py`. */
export function SessionReviewPanel({
  sessionId,
  offer,
  onChange,
}: {
  sessionId: string
  offer: SessionReviewOffer
  onChange: (offer: SessionReviewOffer | null) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const name = offer.name

  async function act(run: (id: string) => Promise<{ offer?: SessionReviewOffer | null }>) {
    setBusy(true)
    setError('')
    try {
      onChange((await run(sessionId)).offer ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  if (offer.ask) {
    const until = new Date(offer.ask.expires_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    return (
      <div className="space-y-2 text-[12px] text-foreground-secondary" data-testid="session-review-live">
        <p>
          You asked {name} to review this session. It can read this one session until {until}, only in{' '}
          <a
            className="text-primary underline"
            href={scopedPath(offer.ask.workspace, `/sessions/${offer.ask.conversation}`)}
          >
            the chat where it replies to you
          </a>.
        </p>
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(withdrawMySessionReview)}
          data-testid="session-review-withdraw"
          className="rounded-md border border-border bg-card px-2 py-1 text-foreground hover:bg-muted disabled:opacity-50"
        >
          {busy ? 'Withdrawing…' : 'Withdraw'}
        </button>
        {error && <p className="text-destructive">{error}</p>}
      </div>
    )
  }

  if (!offer.available) {
    return <p className="text-[12px] text-foreground-secondary" data-testid="session-review-reason">{offer.reason}</p>
  }

  return (
    <div className="space-y-2 text-[12px] text-foreground-secondary" data-testid="session-review-ask">
      <p>
        This opens a chat with {name}, which reads this one session, read-only, and replies to you there. You
        can withdraw it, and it ends by itself after 7 days.
      </p>
      <p>
        It does not turn coaching on, reads no other session of yours, and sends no email. The chat and {name}'s
        working log are yours in canopy, and the machine that runs the turn holds a copy while it works.
      </p>
      {offer.note && <p data-testid="session-review-note">{offer.note}</p>}
      <button
        type="button"
        disabled={busy}
        onClick={() => void act(askMySessionReview)}
        data-testid="session-review-press"
        className="rounded-md bg-primary px-2 py-1 text-primary-foreground hover:opacity-90 disabled:opacity-50"
      >
        {busy ? 'Asking…' : `Ask ${name}`}
      </button>
      {error && <p className="text-destructive">{error}</p>}
    </div>
  )
}
