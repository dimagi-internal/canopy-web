import { useEffect, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import { getThread, type AgentThread, type ThreadMessage } from '@/api/threads'
import { relativeTime } from '@/components/activity/turnLog'
import { MemberAvatar } from '../huddles/MemberAvatar'
import { memberHue } from '../huddles/huddleModel'
import { andList, deJargon, possessive, who } from '../huddles/plainWords'
import {
  audienceOf, budgetWords, messageState, participantsOf, PENDING_WORDS, POSITION_WORDS, resultOf, said, statusWords,
  type Position,
} from './threadModel'

/**
 * One agent thread, for a person seeing it cold: what the conversation is for and
 * who is in it, how it ended, then the messages in order — each one who said it,
 * to whom, where they stand (agrees / suggests a change / doesn't agree / asks)
 * and what they said, opening to the full message and the prompt it answered.
 * Everything is read from the thread and its message turns (apps/threads); polls
 * while the conversation is still going.
 */

const POLL_MS = 15_000

const POSITION_STYLE: Record<Position, string> = {
  agree: 'border-success/40 bg-success/15 text-success',
  counter: 'border-info/40 bg-info/15 text-info',
  decline: 'border-destructive/40 bg-destructive/15 text-destructive',
  question: 'border-warning/40 bg-warning/15 text-warning',
}

function Pill({ className, children }: { className: string; children: ReactNode }) {
  return (
    <span className={`inline-flex h-5 items-center gap-1.5 rounded-full border px-2 text-[11px] font-medium ${className}`}>
      {children}
    </span>
  )
}

function StatusPill({ t }: { t: AgentThread }) {
  if (t.status === 'open') {
    return (
      <Pill className="border-info/30 bg-info/10 text-info">
        <span className="size-1.5 animate-pulse rounded-full bg-info" /> {statusWords(t).toLowerCase()}
      </Pill>
    )
  }
  const agreed = resultOf(t) === 'agreed'
  return (
    <Pill className={agreed ? 'border-success/30 bg-success/10 text-success' : 'border-warning/30 bg-warning/10 text-warning'}>
      {agreed ? 'agreed' : t.status === 'cancelled' ? 'stopped' : 'not agreed'}
    </Pill>
  )
}

/** A revised idea, as an agent offered it: its title, why, and plan. */
function Proposal({ p }: { p: Record<string, unknown> }) {
  const plan = Array.isArray(p.plan) ? p.plan : []
  const rest = Object.entries(p).filter(([k]) => !['title', 'why', 'plan', 'lead', 'with'].includes(k))
  return (
    <div className="space-y-1.5 text-[13px] leading-relaxed text-foreground-secondary">
      {p.title != null && <p className="font-semibold text-foreground">{String(p.title)}</p>}
      {p.why != null && <p><span className="font-medium text-foreground">Why: </span>{deJargon(String(p.why))}</p>}
      {plan.length > 0 && (
        <ol className="list-decimal space-y-0.5 pl-5">{plan.map((s, i) => <li key={i}>{deJargon(String(s))}</li>)}</ol>
      )}
      {rest.map(([k, v]) => (
        <p key={k}><span className="font-medium text-foreground">{k.replace(/_/g, ' ')}: </span>
          {typeof v === 'object' ? JSON.stringify(v) : deJargon(String(v))}</p>
      ))}
    </div>
  )
}

function More({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="group text-[13px]">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[12px] font-medium text-primary hover:underline [&::-webkit-details-marker]:hidden">
        <span aria-hidden className="inline-block transition-transform group-open:rotate-90">▸</span>
        {label}
      </summary>
      <div className="mt-2 rounded-lg border border-border bg-muted/40 p-3 leading-relaxed text-foreground-secondary">{children}</div>
    </details>
  )
}

function clock(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : ''
}

function MessageRow({ t, m, hueOf }: { t: AgentThread; m: ThreadMessage; hueOf: (a: string) => string }) {
  const state = messageState(m)
  const { position, says, proposal } = said(m)
  const to = audienceOf(t, m.speaker)
  return (
    <li data-thread-message={m.n} className="flex gap-3">
      <MemberAvatar slug={m.speaker} hue={hueOf(m.speaker)} />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
          <span className="font-semibold text-foreground">{who(m.speaker)}</span>
          {to.length > 0 && <span className="text-muted-foreground">to {andList(to)}</span>}
          {position && <Pill className={POSITION_STYLE[position]}>{POSITION_WORDS[position]}</Pill>}
          <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{clock(m.finished_at ?? m.created_at)}</span>
        </p>
        <div className="mt-1.5 rounded-xl rounded-tl-sm border border-border bg-card p-3 shadow-sm">
          {state === 'replied' ? (
            <div className="space-y-2">
              {says
                ? <p className="whitespace-pre-wrap text-[14px] leading-relaxed text-foreground">{deJargon(says)}</p>
                : <p className="text-[13px] italic text-muted-foreground">Said nothing in words.</p>}
              {proposal && (
                <div className="rounded-lg border border-border/70 bg-muted/40 p-2.5">
                  <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">Offers this revised idea</p>
                  <Proposal p={proposal} />
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-1">
              <p className="text-[13px] italic text-muted-foreground">{PENDING_WORDS[state]}</p>
              {m.reply_error && <p className="font-mono text-[11px] text-warning">{m.reply_error}</p>}
            </div>
          )}
          {(m.prompt || m.block) && (
            <div className="mt-2 flex flex-col gap-1.5 border-t border-border/60 pt-2">
              {m.block && (
                <More label="The full message">
                  <pre className="whitespace-pre-wrap break-words font-mono text-[11px]">{JSON.stringify(m.block, null, 2)}</pre>
                </More>
              )}
              {m.prompt && (
                <More label={`What ${who(m.speaker)} was asked`}>
                  <p className="whitespace-pre-wrap text-[12px]">{m.prompt}</p>
                </More>
              )}
            </div>
          )}
        </div>
      </div>
    </li>
  )
}

export function ThreadView({ thread: t, now = new Date() }: { thread: AgentThread; now?: Date }) {
  const { workspace = '' } = useParams()
  const people = participantsOf(t)
  const hueOf = (a: string) => memberHue(Math.max(0, people.findIndex((p) => p.agent === a)))
  const parent = (t.parent ?? {}) as Record<string, unknown>
  const huddle = parent.huddle ? String(parent.huddle) : ''
  const outcome = (t.outcome ?? {}) as Record<string, unknown>
  const result = resultOf(t)
  const proposal = outcome.proposal && typeof outcome.proposal === 'object' && Object.keys(outcome.proposal).length
    ? (outcome.proposal as Record<string, unknown>) : null
  const messages = [...(t.messages ?? [])].sort((a, b) => a.n - b.n)
  const left = Math.max(0, t.max_messages - t.messages_used)

  return (
    <div className="pb-28">
      {huddle
        ? <Link to={`/w/${workspace}/huddles/${huddle}`} className="text-[12px] text-muted-foreground hover:text-primary">← The huddle</Link>
        : <Link to={`/w/${workspace}/agents`} className="text-[12px] text-muted-foreground hover:text-primary">← Agents</Link>}

      <header className="mt-3 mb-6 rounded-2xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold text-foreground">{t.purpose}</h1>
          <StatusPill t={t} />
        </div>
        <p className="mt-1.5 text-[14px] leading-relaxed text-foreground-secondary">
          A direct conversation between agents{parent.title ? <> about “{String(parent.title)}”</> : null}, kept short:
          at most {t.max_messages} messages, and {who(t.moderator)} decides when it is done.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[13px] text-foreground-secondary">
          {people.map((p) => (
            <span key={p.agent} className="inline-flex items-center gap-1.5" data-participant={p.agent}>
              <MemberAvatar slug={p.agent} hue={hueOf(p.agent)} size="sm" />
              <span className="font-medium text-foreground">{who(p.agent)}</span>
              {p.role && <span className="text-muted-foreground">({p.role === 'author' ? 'had the idea' : p.role === 'asker' ? 'asked for changes' : p.role})</span>}
            </span>
          ))}
        </div>
        <p className="mt-2 text-[12px] text-muted-foreground">
          started {relativeTime(t.created_at, now)} · {budgetWords(t)}
          {t.status === 'open' && <> · {left} left · {new Date(t.deadline_at) > now ? `ends by ${clock(t.deadline_at)}` : 'past its time limit'}</>}
        </p>
      </header>

      {t.status !== 'open' && (
        <section aria-label="How it ended" data-outcome className={
          'mb-6 rounded-2xl border p-5 ' + (result === 'agreed' ? 'border-success/30 bg-success/5' : 'border-warning/30 bg-warning/5')}>
          <h2 className="text-[13px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">How it ended</h2>
          <p className="mt-1 text-[16px] font-semibold text-foreground">{statusWords(t)}</p>
          {outcome.why != null && String(outcome.why) && <p className="mt-1 text-[14px] text-foreground-secondary">{deJargon(String(outcome.why))}</p>}
          {proposal && (
            <div className="mt-3">
              <p className="mb-1 text-[12px] font-medium text-muted-foreground">The idea as agreed</p>
              <Proposal p={proposal} />
            </div>
          )}
        </section>
      )}

      {t.context && (
        <section aria-label="What it started from" className="mb-6">
          <More label="What the conversation started from">
            <p className="whitespace-pre-wrap text-[12px]">{t.context}</p>
          </More>
        </section>
      )}

      <section aria-label="The conversation">
        <h2 className="mb-3 text-[13px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">The conversation</h2>
        {messages.length === 0 ? (
          <p className="text-[13px] italic text-muted-foreground">
            {t.status === 'open' ? `Nobody has spoken yet — ${possessive(t.moderator)} first message is on its way.` : 'Nobody spoke.'}
          </p>
        ) : (
          <ol className="max-w-[760px] space-y-5">
            {messages.map((m) => <MessageRow key={m.turn_id} t={t} m={m} hueOf={hueOf} />)}
          </ol>
        )}
      </section>
    </div>
  )
}

export function ThreadPage() {
  const { workspace = '', id = '' } = useParams()
  const [thread, setThread] = useState<AgentThread | null>(null)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = () => {
      getThread(id)
        .then((t) => {
          if (!alive) return
          setThread(t)
          setError('')
          setNow(new Date())
          if (t.status === 'open') timer = setTimeout(load, POLL_MS)
        })
        .catch((e: unknown) => {
          if (!alive) return
          setError(e instanceof Error ? e.message : String(e))
          timer = setTimeout(load, POLL_MS * 2)
        })
    }
    load()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [id])

  if (!thread) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <Link to={`/w/${workspace}/agents`} className="text-[12px] text-muted-foreground hover:text-primary">← Agents</Link>
        {error ? <p className="mt-4 text-sm text-destructive">Couldn’t load this conversation: {error}</p> : <div className="mt-4"><WorkbenchSkeleton /></div>}
      </div>
    )
  }
  return (
    <>
      <ThreadView thread={thread} now={now} />
      {error && <p className="mt-4 text-[12px] text-warning">Live refresh paused: {error}</p>}
    </>
  )
}

export default ThreadPage
