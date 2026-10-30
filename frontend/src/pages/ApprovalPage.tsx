import { useEffect, useState, type JSX } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { decideApproval, getApprovalCard, type ApprovalCard } from '@/api/approvals'

const OUTCOME = { approved: 'Approved', declined: 'Declined', closed: 'Closed' } as const

/**
 * One approval card (`/w/:workspace/approvals/:taskKey`) — the page a push, a Slack message and an
 * email all open. Everything needed to decide is on it, above the buttons: who asks, what, the
 * person's own words, the plan, what approving starts and what declining does. `?do=approve` or
 * `?do=decline` (the email's buttons) pre-selects the choice and asks for one more tap — a link
 * never decides by itself.
 */
export function ApprovalPage(): JSX.Element | null {
  const { taskKey } = useParams()
  const [search] = useSearchParams()
  const chosen = search.get('do') === 'approve' || search.get('do') === 'decline' ? search.get('do') : null
  const [card, setCard] = useState<ApprovalCard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!taskKey) return
    let cancelled = false
    getApprovalCard(taskKey)
      .then((c) => !cancelled && setCard(c))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load the card'))
    return () => {
      cancelled = true
    }
  }, [taskKey])

  if (!taskKey) return null
  if (error && !card) return <p className="max-w-2xl text-sm text-destructive">{error}</p>
  if (!card) return <div className="h-40 max-w-2xl animate-pulse rounded-lg bg-muted" />

  const decide = async (decision: 'approve' | 'decline') => {
    setBusy(true)
    setError(null)
    try {
      setCard(await decideApproval(card.key, decision, comment.trim()))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
      // Someone else may have decided first: show what happened.
      getApprovalCard(card.key).then(setCard).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  const who = card.agent_name || card.agent_slug || card.filed_by || 'An agent'
  return (
    <div className="max-w-2xl space-y-4" data-testid="approval-card">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The proposal">
        <p className="text-[12px] text-muted-foreground">{who} is asking you to decide</p>
        <h1 className="mt-1 text-lg font-semibold text-foreground">{card.title}</h1>
        {card.summary && <p className="mt-2 text-[14px] text-foreground whitespace-pre-wrap">{card.summary}</p>}

        {card.evidence.length > 0 && (
          <div className="mt-4 space-y-2" data-testid="approval-evidence">
            <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
              What they said
            </h2>
            {card.evidence.map((e, i) => (
              <blockquote key={i} className="border-l-2 border-border pl-3 text-[13px] text-foreground">
                <span className="whitespace-pre-wrap">{e.quote}</span>
                <footer className="mt-0.5 text-[12px] text-muted-foreground">
                  {e.who}
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

        {card.rationale && (
          <div className="mt-4">
            <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Why</h2>
            <p className="mt-1 text-[13px] text-foreground whitespace-pre-wrap">{card.rationale}</p>
          </div>
        )}

        {card.plan.length > 0 && (
          <div className="mt-4" data-testid="approval-plan">
            <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
              What will be done
            </h2>
            <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-[13px] text-foreground">
              {card.plan.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </div>
        )}

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div data-testid="approval-if-approve">
            <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
              If you approve
            </h2>
            {card.will_run.length === 0 ? (
              <p className="mt-1 text-[13px] text-foreground">It moves to In progress.</p>
            ) : (
              <ul className="mt-1 space-y-1 text-[13px] text-foreground">
                {card.will_run.map((w, i) => (
                  <li key={i}>
                    <span className="font-mono text-[12px]">{w.agent}</span> starts: {w.prompt}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div data-testid="approval-if-decline">
            <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
              If you decline
            </h2>
            <p className="mt-1 text-[13px] text-foreground">{card.if_declined}</p>
          </div>
        </div>

        {(card.links.length > 0 || card.source_url) && (
          <p className="mt-4 flex flex-wrap gap-x-3 text-[12px]">
            {card.source_url && (
              <a href={card.source_url} className="text-primary hover:underline" target="_blank" rel="noreferrer">
                Where it came from
              </a>
            )}
            {card.links.map((l) => (
              <a key={l.url} href={l.url} className="text-primary hover:underline" target="_blank" rel="noreferrer">
                {l.label}
              </a>
            ))}
          </p>
        )}
        {card.sent_to.length > 0 && (
          <p className="mt-3 text-[11px] text-muted-foreground">Sent to {card.sent_to.join(', ')}.</p>
        )}
      </section>

      {card.state === 'open' ? (
        <section className="rounded-lg border border-border bg-card p-4" aria-label="Decide">
          {chosen && (
            <p className="mb-2 text-[13px] text-foreground" data-testid="approval-confirm">
              You chose to <b>{chosen}</b> this. Tap {chosen === 'approve' ? 'Approve' : 'Decline'} to confirm.
            </p>
          )}
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="A note for the agent (optional)"
            aria-label="A note for the agent"
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
          <b>{OUTCOME[card.state]}</b>
          {card.decided && (
            <>
              {' '}
              by {card.decided.by}
              {card.decided.via ? ` via ${card.decided.via}` : ''}.
              {card.decided.comment && <span className="block text-muted-foreground">“{card.decided.comment}”</span>}
            </>
          )}
          {error && <p className="mt-2 text-destructive">{error}</p>}
        </section>
      )}
    </div>
  )
}
