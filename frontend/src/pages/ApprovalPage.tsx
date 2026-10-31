import { useEffect, useState, type JSX } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { decideApproval, getApproval, type ApprovalCard } from '@/api/approvals'

const OUTCOME: Record<ApprovalCard['status'], string> = {
  pending: 'Waiting',
  done: 'Approved and done',
  failed: 'Approved, but it could not be done',
  declined: 'Declined',
  cancelled: 'Withdrawn',
  expired: 'Expired',
}

/**
 * One approval request (`/w/:workspace/approvals/:id`) — the page a push, a Slack message, an email and
 * an alert all open. What approving WILL DO is the server's rendering of the request's typed params
 * and sits first; the requester's own words are kept apart and labelled as theirs. `?do=approve|decline`
 * (the email's buttons) pre-selects the choice and asks for one more tap — a link never decides by itself.
 */
export function ApprovalPage(): JSX.Element | null {
  const { approvalId } = useParams()
  const [search] = useSearchParams()
  const chosen = search.get('do') === 'approve' || search.get('do') === 'decline' ? search.get('do') : null
  const [card, setCard] = useState<ApprovalCard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!approvalId) return
    let cancelled = false
    getApproval(approvalId)
      .then((c) => !cancelled && setCard(c))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load the request'))
    return () => {
      cancelled = true
    }
  }, [approvalId])

  if (!approvalId) return null
  if (error && !card) return <p className="max-w-2xl text-sm text-destructive">{error}</p>
  if (!card) return <div className="h-40 max-w-2xl animate-pulse rounded-lg bg-muted" />

  const decide = async (decision: 'approve' | 'decline') => {
    setBusy(true)
    setError(null)
    try {
      setCard(await decideApproval(card.id, decision, comment.trim()))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
      // Someone else may have decided first: show what happened.
      getApproval(card.id).then(setCard).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-4" data-testid="approval-card">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The request">
        <p className="text-[12px] text-muted-foreground" data-testid="approval-kind">
          {card.kind_label} · asked by {card.requested_by || 'someone'}
        </p>
        <h1 className="mt-1 text-lg font-semibold text-foreground">{card.title}</h1>

        <div className="mt-4" data-testid="approval-plan">
          <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            If you approve, this happens
          </h2>
          <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-[13px] text-foreground">
            {card.plan.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
          <p className="mt-2 text-[12px] text-muted-foreground">
            This is exactly what runs — canopy does it as you, with no agent involved
            {card.kind === 'agent.run_turns' ? ', except this kind, which starts the turns listed' : ''}.
          </p>
        </div>

        <div className="mt-3" data-testid="approval-if-decline">
          <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            If you decline
          </h2>
          <p className="mt-1 text-[13px] text-foreground">{card.if_declined}</p>
        </div>

        {(card.reason || card.evidence.length > 0) && (
          <div className="mt-5 rounded-md border border-border bg-muted/40 p-3" data-testid="approval-theirs">
            <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
              In {card.requested_by || 'their'} own words
            </h2>
            {card.reason && <p className="mt-1 text-[13px] text-foreground whitespace-pre-wrap">{card.reason}</p>}
            {card.evidence.map((e, i) => (
              <blockquote key={i} className="mt-2 border-l-2 border-border pl-3 text-[13px] text-foreground">
                <span className="whitespace-pre-wrap">{e.quote}</span>
                <footer className="mt-0.5 text-[12px] text-muted-foreground">
                  {e.who || 'someone'}
                  {e.source_url && (
                    <>
                      {' · '}
                      <a href={e.source_url} className="text-primary hover:underline" target="_blank" rel="noreferrer">
                        source
                      </a>
                    </>
                  )}
                </footer>
              </blockquote>
            ))}
          </div>
        )}

        <p className="mt-3 text-[11px] text-muted-foreground">
          Needs {card.needs}.
          {card.sent_to.length > 0 && <> Sent to {card.sent_to.join(', ')}.</>}
        </p>
      </section>

      {card.status === 'pending' ? (
        <section className="rounded-lg border border-border bg-card p-4" aria-label="Decide">
          {chosen && (
            <p className="mb-2 text-[13px] text-foreground" data-testid="approval-confirm">
              You chose to <b>{chosen}</b> this. Tap {chosen === 'approve' ? 'Approve' : 'Decline'} to confirm.
            </p>
          )}
          {!card.can_decide && (
            <p className="mb-2 text-[13px] text-muted-foreground" data-testid="approval-cannot">
              You can look, but this needs {card.needs}.
            </p>
          )}
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="A note (optional)"
            aria-label="A note"
            rows={2}
            className="w-full rounded-md border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
          />
          {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              variant={chosen === 'decline' ? 'outline' : 'default'}
              disabled={busy || !card.can_decide}
              data-testid="approval-approve"
              onClick={() => void decide('approve')}
            >
              Approve
            </Button>
            <Button
              variant={chosen === 'decline' ? 'default' : 'outline'}
              disabled={busy || !card.can_decide}
              data-testid="approval-decline"
              onClick={() => void decide('decline')}
            >
              Decline
            </Button>
          </div>
        </section>
      ) : (
        <section className="rounded-lg border border-border bg-card p-4 text-[13px]" data-testid="approval-outcome">
          <b>{OUTCOME[card.status]}</b> — {card.outcome}
          {card.decided?.comment && <span className="block text-muted-foreground">“{card.decided.comment}”</span>}
          {card.status === 'failed' && <p className="mt-1 text-destructive">{card.error}</p>}
          {error && <p className="mt-2 text-destructive">{error}</p>}
          <ol className="mt-3 space-y-0.5 text-[12px] text-muted-foreground" data-testid="approval-history">
            {card.history.map((h, i) => (
              <li key={i}>
                {h.kind}
                {h.by ? ` · ${h.by}` : ''}
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  )
}
