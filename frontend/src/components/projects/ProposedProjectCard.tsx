import { useState, type JSX } from 'react'
import { Link } from 'react-router-dom'

import { actOnProposal, AgentApiError, type ProposedProjectOut } from '@/api/agents'
import { StatusChip } from '@/pages/agents/projectParts'

import { BackingControl } from './BackingControl'
import { BackingList } from './BackingList'

// A PROPOSED PROJECT, as people act on it — the one component the project page
// and the huddle page both render, so a person backs, comments and decides in
// whichever place they found it (spec 2026-10-16-work-group-huddles-design.md).
//
// What it is (name, done-when, why, plan, partners), then the decision — Start /
// Decline for whoever may decide (`may_decide`: a workspace owner or admin, or
// the project's owner), a plain "waiting on …" line for everyone else — then
// where people stand (BackingList + BackingControl) and the comment thread,
// which is the decision card's replies. No new verbs: Start and Decline are the
// card's approve and decline, a comment is its reply.

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof AgentApiError && err.detail ? err.detail : fallback
}

function Decision({ p, onChanged }: { p: ProposedProjectOut; onChanged: () => void }): JSX.Element | null {
  const [declining, setDeclining] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (p.status !== 'proposed' || !p.decision_open) return null
  if (!p.may_decide) {
    return (
      <p className="text-[12px] text-muted-foreground">
        Waiting on {p.owner_email ?? 'a workspace admin'} to start or decline it
        {p.expires_at ? ` — it lapses on ${when(p.expires_at)} if nobody does` : ''}.
      </p>
    )
  }
  const decide = (action: 'approve' | 'decline') => {
    setBusy(true)
    setError(null)
    void actOnProposal(p, action, action === 'decline' ? reason.trim() : '')
      .then(onChanged)
      .catch((err: unknown) => setError(errorText(err, 'Could not record that')))
      .finally(() => setBusy(false))
  }
  return (
    <div className="space-y-2" data-decision>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => decide('approve')}
          className="min-h-11 rounded bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 sm:min-h-0"
        >
          Start this project
        </button>
        <button
          type="button"
          disabled={busy}
          aria-pressed={declining}
          onClick={() => setDeclining((d) => !d)}
          className="min-h-11 rounded border border-border px-3 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50 sm:min-h-0"
        >
          Decline
        </button>
        {p.expires_at && (
          <span className="text-[11px] text-muted-foreground">Lapses {when(p.expires_at)} if nobody decides.</span>
        )}
      </div>
      {declining && (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            decide('decline')
          }}
        >
          <input
            aria-label="Why decline"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why — the agent keeps the reason"
            className="min-h-11 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground sm:min-h-0 sm:min-w-64"
          />
          <button
            type="submit"
            disabled={busy}
            className="min-h-11 rounded bg-destructive px-2 py-1 text-[12px] text-destructive-foreground disabled:opacity-50 sm:min-h-0"
          >
            Decline it
          </button>
        </form>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}

function Comments({ p, onChanged }: { p: ProposedProjectOut; onChanged: () => void }): JSX.Element | null {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!p.decision_task) return null
  const comments = p.comments ?? []
  return (
    <div className="space-y-2">
      {comments.length > 0 && (
        <ul aria-label="Comments" className="space-y-1.5 text-[13px]">
          {comments.map((c, i) => (
            <li key={i} className="leading-snug">
              <span className="font-medium text-foreground">{c.by}</span>{' '}
              <span className="text-[11px] text-muted-foreground">{when(c.at)}</span>
              <p className="whitespace-pre-wrap text-foreground-secondary">{c.comment}</p>
            </li>
          ))}
        </ul>
      )}
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (!text.trim()) return
          setBusy(true)
          setError(null)
          void actOnProposal(p, 'reply', text.trim())
            .then(() => {
              setText('')
              onChanged()
            })
            .catch((err: unknown) => setError(errorText(err, 'Could not post that')))
            .finally(() => setBusy(false))
        }}
      >
        <input
          aria-label="Comment"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={`Reply to ${p.agent} — a question, a change, a concern`}
          className="min-h-11 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground sm:min-h-0 sm:min-w-64"
        />
        <button
          type="submit"
          disabled={busy || !text.trim()}
          className="min-h-11 rounded border border-border px-2 py-1 text-[12px] text-foreground hover:bg-muted disabled:opacity-50 sm:min-h-0"
        >
          Reply
        </button>
      </form>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}

export function ProposedProjectCard({
  proposal: p,
  onChanged,
  linkToProject = false,
}: {
  proposal: ProposedProjectOut
  onChanged: () => void
  /** On the huddle page the name links to the project's own page. */
  linkToProject?: boolean
}): JSX.Element {
  const backings = p.backings ?? []
  const plan = p.plan ?? []
  const partners = p.partners ?? []
  const mine = backings.find((b) => b.mine)
  const declined = p.status === 'archived' && p.notes.includes('Declined:')
  return (
    <article
      data-proposal={p.ref}
      className="space-y-3 rounded-xl border border-border bg-card p-4"
      aria-label={`Proposed project: ${p.name}`}
    >
      <header className="space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[15px] font-semibold text-foreground">
            {linkToProject ? (
              <Link to={p.url} className="hover:text-primary hover:underline">
                {p.name}
              </Link>
            ) : (
              p.name
            )}
          </h3>
          <StatusChip status={p.status} />
          <span className="text-[11px] text-muted-foreground">
            {p.agent} · {p.ref}
            {p.proposed_at ? ` · proposed ${when(p.proposed_at)}` : ''}
          </span>
        </div>
        <p className="text-[13px] text-foreground">
          <span className="text-muted-foreground">Done when: </span>
          {p.outcome}
        </p>
      </header>

      {p.why && <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground-secondary">{p.why}</p>}

      {plan.length > 0 && (
        <ol aria-label="Plan" className="ml-5 list-decimal space-y-0.5 text-[13px] text-foreground-secondary">
          {plan.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}

      {partners.length > 0 && (
        <p className="text-[12px] text-muted-foreground">
          With{' '}
          {partners.map((t, i) => (
            <span key={t.task_id}>
              {i > 0 && ', '}
              <Link to={t.url} className="text-foreground hover:text-primary hover:underline">
                {t.agent}
              </Link>{' '}
              ({t.status.replace('_', ' ')})
            </span>
          ))}
        </p>
      )}

      {declined && (
        <p className="text-[12px] text-muted-foreground">{p.notes.slice(p.notes.lastIndexOf('Declined:'))}</p>
      )}
      <Decision p={p} onChanged={onChanged} />

      <section aria-label="Where people stand" className="space-y-2 border-t border-border pt-3">
        <BackingList backings={backings} />
        {p.status === 'proposed' && <BackingControl project={p} mine={mine} onChanged={onChanged} />}
      </section>

      <section aria-label="Comments" className="border-t border-border pt-3">
        <Comments p={p} onChanged={onChanged} />
      </section>
    </article>
  )
}
