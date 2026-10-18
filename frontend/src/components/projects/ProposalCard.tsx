import { useState, type JSX } from 'react'
import { Link } from 'react-router-dom'

import {
  actOnProposal,
  actOnTaskProposal,
  AgentApiError,
  backProject,
  backTask,
  unbackProject,
  unbackTask,
  type BackingStance,
  type ProjectBackingOut,
  type ProposedProjectOut,
  type ProposedTaskOut,
} from '@/api/agents'
import { StatusChip } from '@/pages/agents/projectParts'

import { BackingControl } from './BackingControl'
import { BackingList } from './BackingList'

// A PROPOSAL, as people act on it — a proposed PROJECT or a check-in's proposed
// TASK. The one card the project page and the huddle page both render, so a
// person backs, comments and decides in whichever place they found it (spec
// 2026-10-17-project-check-ins-design.md).
//
// What it is (name, done-when, why, plan, partners), then the decision — Start /
// Decline for whoever may decide (`may_decide`), a plain "waiting on …" line for
// everyone else — then where people stand (BackingList + BackingControl) and the
// comment thread. No new verbs: for a project, Start and Decline are its decision
// card's approve and decline and a comment is the card's reply; for a task they
// are the task's own.

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof AgentApiError && err.detail ? err.detail : fallback
}

type Action = 'approve' | 'decline' | 'reply'
type Comment = ProposedProjectOut['comments'] extends readonly (infer C)[] | undefined ? C : never
type Partner = ProposedProjectOut['partners'] extends readonly (infer P)[] | undefined ? P : never

/** What the card needs from either kind of proposal. */
interface View {
  key: string
  noun: 'project' | 'task'
  name: string
  status: string
  agent: string
  ref: string
  proposedAt: string | null | undefined
  expiresAt: string | null | undefined
  doneWhen: string
  why: string
  plan: readonly string[]
  partners: readonly Partner[]
  backings: readonly ProjectBackingOut[]
  comments: readonly Comment[]
  notes: string
  /** A decision is still to be made on it. */
  open: boolean
  mayDecide: boolean
  waitingOn: string
  /** Where people may still back it. */
  backable: boolean
  canComment: boolean
  url: string
  act: (action: Action, comment: string) => Promise<unknown>
  back: (stance: BackingStance, line: string) => Promise<unknown>
  unback: () => Promise<unknown>
  extra: JSX.Element | null
}

function projectView(p: ProposedProjectOut): View {
  const from = p.spawned_from
  return {
    key: `project-${p.agent}-${p.ref}`, noun: 'project', name: p.name, status: p.status, agent: p.agent, ref: p.ref,
    proposedAt: p.proposed_at, expiresAt: p.expires_at, doneWhen: p.outcome, why: p.why ?? '',
    plan: p.plan ?? [], partners: p.partners ?? [], backings: p.backings ?? [], comments: p.comments ?? [],
    notes: p.notes, open: p.status === 'proposed' && !!p.decision_open, mayDecide: !!p.may_decide,
    waitingOn: p.owner_email ?? 'a workspace admin', backable: p.status === 'proposed',
    canComment: !!p.decision_task, url: p.url,
    act: (action, comment) => actOnProposal(p, action, comment),
    back: (stance, line) => backProject(p, stance, line),
    unback: () => unbackProject(p),
    extra: from ? (
      <p className="text-[12px] text-muted-foreground">
        From{' '}
        <Link to={from.url} className="text-foreground hover:text-primary hover:underline">
          {from.ref} {from.title}
        </Link>
        {from.project_name ? ` in ${from.project_name}` : ''}
      </p>
    ) : null,
  }
}

function taskView(t: ProposedTaskOut): View {
  const spawned = t.spawned ?? []
  return {
    key: `task-${t.agent}-${t.ref}`, noun: 'task', name: t.title, status: t.status, agent: t.agent, ref: t.ref,
    proposedAt: t.proposed_at, expiresAt: t.expires_at, doneWhen: t.done_when ?? '', why: t.why ?? '',
    plan: t.plan ?? [], partners: t.partners ?? [], backings: t.backings ?? [], comments: t.comments ?? [],
    notes: t.notes ?? '', open: !!t.decision_open, mayDecide: !!t.may_decide,
    waitingOn: "the project's owner or the check-in's lead", backable: t.status === 'suggested', canComment: true,
    url: t.url,
    act: (action, comment) => actOnTaskProposal(t, action, comment),
    back: (stance, line) => backTask(t, stance, line),
    unback: () => unbackTask(t),
    extra: spawned.length ? (
      <p className="text-[12px] text-muted-foreground">
        Spawned{' '}
        {spawned.map((p, i) => (
          <span key={`${p.agent}-${p.ref}`}>
            {i > 0 && ', '}→{' '}
            <Link to={p.url} className="text-foreground hover:text-primary hover:underline">
              {p.name}
            </Link>{' '}
            ({p.status})
          </span>
        ))}
      </p>
    ) : null,
  }
}

function Decision({ v, onChanged }: { v: View; onChanged: () => void }): JSX.Element | null {
  const [declining, setDeclining] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!v.open) return null
  if (!v.mayDecide) {
    return (
      <p className="text-[12px] text-muted-foreground">
        Waiting on {v.waitingOn} to start or decline it
        {v.expiresAt ? ` — it lapses on ${when(v.expiresAt)} if nobody does` : ''}.
      </p>
    )
  }
  const decide = (action: 'approve' | 'decline') => {
    setBusy(true)
    setError(null)
    void v
      .act(action, action === 'decline' ? reason.trim() : '')
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
          Start this {v.noun}
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
        {v.expiresAt && (
          <span className="text-[11px] text-muted-foreground">Lapses {when(v.expiresAt)} if nobody decides.</span>
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

function Comments({ v, onChanged }: { v: View; onChanged: () => void }): JSX.Element | null {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!v.canComment) return null
  return (
    <div className="space-y-2">
      {v.comments.length > 0 && (
        <ul aria-label="Comments" className="space-y-1.5 text-[13px]">
          {v.comments.map((c, i) => (
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
          void v
            .act('reply', text.trim())
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
          placeholder={`Reply to ${v.agent} — a question, a change, a concern`}
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

/** A proposed project (`proposal`) or a check-in's proposed task (`task`) — pass one. */
export function ProposalCard({
  proposal,
  task,
  onChanged,
  linkToProject = false,
}: {
  proposal?: ProposedProjectOut
  task?: ProposedTaskOut
  onChanged: () => void
  /** On the huddle page the name links to the project's own page. */
  linkToProject?: boolean
}): JSX.Element | null {
  const v = proposal ? projectView(proposal) : task ? taskView(task) : null
  if (!v) return null
  const mine = v.backings.find((b) => b.mine)
  const declined = (v.status === 'archived' || v.status === 'declined') && v.notes.includes('Declined:')
  return (
    <article
      data-proposal={v.ref}
      data-kind={v.noun}
      className="space-y-3 rounded-xl border border-border bg-card p-4"
      aria-label={`Proposed ${v.noun}: ${v.name}`}
    >
      <header className="space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[15px] font-semibold text-foreground">
            {linkToProject ? (
              <Link to={v.url} className="hover:text-primary hover:underline">
                {v.name}
              </Link>
            ) : (
              v.name
            )}
          </h3>
          <StatusChip status={v.status} />
          <span className="text-[11px] text-muted-foreground">
            {v.agent} · {v.ref}
            {task?.project_name ? ` · in ${task.project_name}` : ''}
            {v.proposedAt ? ` · proposed ${when(v.proposedAt)}` : ''}
          </span>
        </div>
        <p className="text-[13px] text-foreground">
          <span className="text-muted-foreground">Done when: </span>
          {v.doneWhen}
        </p>
      </header>

      {v.why && <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground-secondary">{v.why}</p>}

      {v.plan.length > 0 && (
        <ol aria-label="Plan" className="ml-5 list-decimal space-y-0.5 text-[13px] text-foreground-secondary">
          {v.plan.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}

      {v.partners.length > 0 && (
        <p className="text-[12px] text-muted-foreground">
          With{' '}
          {v.partners.map((t, i) => (
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
      {v.extra}

      {declined && (
        <p className="text-[12px] text-muted-foreground">{v.notes.slice(v.notes.lastIndexOf('Declined:'))}</p>
      )}
      <Decision v={v} onChanged={onChanged} />

      <section aria-label="Where people stand" className="space-y-2 border-t border-border pt-3">
        <BackingList backings={v.backings} />
        {v.backable && <BackingControl save={v.back} withdraw={v.unback} mine={mine} onChanged={onChanged} />}
      </section>

      <section aria-label="Comments" className="border-t border-border pt-3">
        <Comments v={v} onChanged={onChanged} />
      </section>
    </article>
  )
}
