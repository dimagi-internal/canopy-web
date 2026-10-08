import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import type { Huddle, HuddleCell } from '@/api/huddles'
import { AnswerPill } from './BlockView'
import { HuddleSequence } from './HuddleSequence'
import { MemberAvatar } from './MemberAvatar'
import { Linkified } from './HuddleOutcome'
import { HuddleStory } from './HuddleStory'
import { costWords, failsWords, reportItemWords, servesWords } from './briefWords'
import { useBrief } from './briefContext'
import {
  pairQA, proposalThreads, readView, threadFor,
  type ProposalThread, type RawProposal, type Reply, type ThreadRound,
} from './conversationModel'
import {
  cellState, columns, holdWords, leaderAsks, memberHue, normAnswer, normResolution, roundsToShow, sizeWords,
  type Block,
} from './huddleModel'
import {
  andList, boardLinkText, nextStep, peopleWords, possessive, resolutionWords, stepHeading, taskStatusPlain, VERDICT_WORDS, who,
} from './plainWords'

/**
 * The conversation, to READ: "By agent" is one member's thread with the leader
 * as a chat transcript — what the leader asked, what the member said back, and
 * what teammates said about the member's proposals; "By proposal" follows each
 * proposal from pitch to critique to the partners' answers to what it became.
 * "Map" is the rounds × members grid with its co-sign arcs, unchanged.
 *
 * The view and the selected member live in the URL (`?view=` / `?with=`), so a
 * link opens the same thread. Full text everywhere: a long bubble is clipped
 * to about thirteen lines with "show more", never cut with an ellipsis.
 */

/** The three ways to read a huddle. "Full conversation" is two views (by
 * agent / by idea) under one button; `?view=` keeps whichever is open. */
const VIEWS: { id: 'story' | 'full' | 'map'; label: string }[] = [
  { id: 'story', label: 'Story' },
  { id: 'full', label: 'Full conversation' },
  { id: 'map', label: 'Diagram' },
]
const FULL_VIEWS: { id: 'agent' | 'proposal'; label: string }[] = [
  { id: 'agent', label: 'By agent' },
  { id: 'proposal', label: 'By idea' },
]

const LEADER_HUE = 'var(--foreground-secondary)'
const str = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'string' ? v : String(v))
function list<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

// ── bubbles ──────────────────────────────────────────────────────────────────

/** Clips a long bubble to ~13 lines with a "show more"; the full text stays in
 * the page (find-in-page works), only its height is limited. */
function Clamp({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  // 'fits' unless the content runs well past the limit: clipping the last line
  // or two behind a "show more" costs a click for nothing.
  const [mode, setMode] = useState<'fits' | 'long'>('fits')
  const [open, setOpen] = useState(false)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || open) return
    // Measured a frame later, once fonts and wrapping have settled.
    const id = requestAnimationFrame(() => {
      const limit = parseFloat(getComputedStyle(el).fontSize || '14') * 1.65 * 13
      setMode(el.scrollHeight > limit * 1.35 ? 'long' : 'fits')
    })
    return () => cancelAnimationFrame(id)
  }, [children, open])
  const clipped = mode === 'long' && !open
  return (
    <div>
      <div
        ref={ref}
        data-clamped={clipped ? '' : undefined}
        className={'relative ' + (clipped ? 'max-h-[21rem] overflow-hidden' : '')}
      >
        {children}
        {clipped && (
          <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-[var(--bubble-bg)] to-transparent" />
        )}
      </div>
      {mode === 'long' && (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="mt-1 text-[12px] font-medium text-primary hover:underline"
        >
          {open ? 'show less' : 'show more'}
        </button>
      )}
    </div>
  )
}

function Bubble({ who, hue, side, label, children, data, tint = side === 'right', clamp = true }: {
  who: string
  /** False for a proposal's pitch: it is read in full, never clipped. */
  clamp?: boolean
  /** Wash the bubble in the speaker's colour (members yes, the leader no). */
  tint?: boolean
  hue: string
  side: 'left' | 'right'
  /** Shown above the bubble: "ada", "eva on your proposal …". */
  label: ReactNode
  children: ReactNode
  data?: string
}) {
  const bg = tint ? `color-mix(in oklch, ${hue} 9%, var(--card))` : 'var(--card)'
  return (
    <div data-bubble={data ?? who} className={'flex items-end gap-2 ' + (side === 'right' ? 'flex-row-reverse' : '')}>
      <MemberAvatar slug={who} hue={hue} size="sm" />
      <div className={'min-w-0 max-w-[min(100%,36rem)] ' + (side === 'right' ? 'items-end' : '')}>
        {label ? <div className={'mb-1 px-1 text-[11px] text-muted-foreground ' + (side === 'right' ? 'text-right' : '')}>{label}</div> : null}
        <div
          className={'rounded-2xl border px-4 py-3 text-[14px] leading-relaxed text-foreground ' + (side === 'right' ? 'rounded-br-md' : 'rounded-bl-md')}
          style={{
            background: bg,
            borderColor: tint ? `color-mix(in oklch, ${hue} 35%, var(--border))` : 'var(--border)',
            ['--bubble-bg' as string]: bg,
          }}
        >
          {clamp ? <Clamp>{children}</Clamp> : children}
        </div>
      </div>
    </div>
  )
}

function Part({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mt-3 first:mt-0">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</div>
      {children}
    </div>
  )
}

function Bullets({ items }: { items: unknown[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5 marker:text-muted-foreground">
      {items.map((it, i) => <li key={i} className="whitespace-pre-wrap break-words"><Linkified text={str(typeof it === 'object' ? JSON.stringify(it) : it)} /></li>)}
    </ul>
  )
}

function FullMessage({ prompt, leader }: { prompt: string; leader: string }) {
  if (!prompt) return null
  return (
    <details className="mt-2 text-[12px]">
      <summary className="cursor-pointer text-muted-foreground hover:text-foreground">see {possessive(leader)} exact message</summary>
      <pre className="mt-2 max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted/50 p-3 font-mono text-[11px] leading-relaxed text-foreground-secondary">
        {prompt}
      </pre>
    </details>
  )
}

// ── a proposal, readable ─────────────────────────────────────────────────────

export function PitchCard({ p, proposedBy, hueOf, showTitle = true }: { p: RawProposal; proposedBy: string; hueOf: (m: string) => string; showTitle?: boolean }) {
  const lead = str(p.lead) || proposedBy
  const partners = [...new Set(list<unknown>(p.with).map(str))].filter((m) => m && m !== lead)
  const project = p.project && typeof p.project === 'object' ? (p.project as { name?: unknown; new?: unknown }) : null
  const projectName = project ? str(project.name) : str(p.project)
  const size = sizeWords(str(p.effort), typeof p.confidence === 'number' ? p.confidence : null)
  const serves = servesWords(p.priority, useBrief())
  const cost = costWords(p.cost_to_jonathan)
  const fails = failsWords(p.fails_if)
  const asks = p.ask_of_partners && typeof p.ask_of_partners === 'object' ? Object.entries(p.ask_of_partners as Record<string, unknown>) : []
  return (
    <article data-pitch={str(p.title)}>
      {showTitle && <h5 className="mb-1 text-[15px] font-semibold leading-snug text-foreground">{str(p.title) || 'Untitled idea'}</h5>}
      <p className="text-[12px] text-muted-foreground">
        {lead !== proposedBy ? <>suggested by {who(proposedBy)}, with <span className="font-medium text-foreground-secondary">{who(lead)}</span> leading</> : <>{peopleWords(lead, partners)}</>}
        {lead !== proposedBy && partners.length > 0 ? <>, together with {andList(partners)}</> : null}
        {size && <> · {size}</>}
      </p>
      {(serves || projectName) && (
        <p className="mt-2 text-[13px] leading-snug">
          {serves && <><span className="font-medium text-foreground-secondary">{serves.label}{serves.text ? ':' : ''}</span> {serves.text}</>}
          {projectName && <span className="text-muted-foreground">{serves ? ' · ' : ''}project {projectName}{project?.new ? ' (new)' : ''}</span>}
        </p>
      )}
      {str(p.why) && <p className="mt-2 whitespace-pre-wrap text-[13px] leading-relaxed"><span className="font-medium text-foreground-secondary">Why: </span>{str(p.why)}</p>}
      {list(p.plan).length > 0 && (
        <div className="mt-2 text-[13px]">
          <div className="font-medium text-foreground-secondary">Plan</div>
          <ol className="mt-0.5 list-decimal space-y-0.5 pl-5">
            {list(p.plan).map((s, i) => <li key={i}>{str(s)}</li>)}
          </ol>
        </div>
      )}
      {asks.length > 0 && (
        <div className="mt-2 space-y-1 text-[13px]">
          {asks.map(([m, ask]) => (
            <p key={m}><span className="font-medium" style={{ color: hueOf(m) }}>Needs from {who(m)}:</span> {str(ask)}</p>
          ))}
        </div>
      )}
      {str(p.success_measure) && (
        <p className="mt-2 text-[13px] text-foreground-secondary"><span className="font-medium">Done when: </span>{str(p.success_measure)}</p>
      )}
      {cost && <p data-needs-you className="mt-2 text-[13px] text-foreground-secondary"><span className="font-medium">Needs from you: </span>{cost}</p>}
      {fails && <p data-fails-if className="mt-1 text-[13px] text-foreground-secondary">{fails}</p>}
    </article>
  )
}

// ── By agent ─────────────────────────────────────────────────────────────────
//
// Each round reads as a run of short messages rather than one long one: the
// leader's ask, then the member's reply split where it naturally splits (a
// report section, an answer to one question, one proposal), and in round 3 the
// leader's critique of each joint proposal followed straight away by the
// member's answer to it — so the back-and-forth sits side by side.

const normTitle = (s: unknown) => str(s).toLowerCase().replace(/\W+/g, ' ').trim()

function NoReply({ cell, member }: { cell: HuddleCell | undefined; member: string }) {
  if (!cell) return <p className="text-[13px] italic text-muted-foreground">{who(member)} was not asked at this step.</p>
  const s = cellState(cell)
  const words = {
    hidden: `${who(member)} answered, but the answer is hidden from you — you can see that it ran.`,
    waiting: `Waiting for ${possessive(member)} answer…`,
    failed: `${possessive(member)} session didn't finish before it answered.`,
    'no-reply': `No answer from ${who(member)}.`,
    replied: '',
  }[s]
  return <p className="text-[13px] italic text-muted-foreground">{words}</p>
}

function FeedbackLine({ member, text }: { member: string; text: string }) {
  if (!text) return null
  return (
    <p data-feedback={member} className="ml-auto max-w-[min(100%,36rem)] pr-9 text-right text-[12px] italic leading-snug text-muted-foreground">
      {possessive(member)} note on how this went: {text}
    </p>
  )
}

const REPORT_PARTS: [string, string][] = [
  ['state', "What I'm mid-way through"], ['levers', 'Where I can move the priorities'],
  ['worked_on', 'What I worked on'], ['priorities', "What I think Jonathan's priorities are"], ['projects', 'Projects'],
  ['offers', 'What I can offer'], ['needs', 'What I need'],
]

function ReportPart({ k, b }: { k: string; b: Block }) {
  const brief = useBrief()
  if (k !== 'projects') return <Bullets items={list(b[k]).map((it) => reportItemWords(k, it, brief))} />
  return (
    <ul className="space-y-1.5">
      {list<unknown>(b.projects).map((raw, i) => {
        const p = (raw && typeof raw === 'object' ? raw : { name: raw }) as Record<string, unknown>
        return (
          <li key={i}>
            <span className="font-medium">{str(p.name)}</span>
            {str(p.state) && <span className="text-foreground-secondary"> — {str(p.state)}</span>}
            {str(p.next) && <span className="block text-[13px] text-muted-foreground">Next: {str(p.next)}</span>}
          </li>
        )
      })}
    </ul>
  )
}

function AnswerBody({ a }: { a: Record<string, unknown> }) {
  return (
    <>
      <p className="flex flex-wrap items-center gap-2">
        <AnswerPill answer={normAnswer(a.answer)} />
        <span className="font-medium">{str(a.title)}</span>
        {str(a.lead) && <span className="text-[12px] text-muted-foreground">led by {who(str(a.lead))}</span>}
      </p>
      {str(a.note) && <p className="mt-1 whitespace-pre-wrap">{str(a.note)}</p>}
    </>
  )
}

function ResolutionPill({ v }: { v: 'accept' | 'reject' | null }) {
  if (!v) return null
  const tone = v === 'accept' ? 'bg-success/15 text-success border-success/40' : 'bg-destructive/15 text-destructive border-destructive/40'
  return <span className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium ${tone}`}>{resolutionWords(v)}</span>
}

function GenericBody({ b }: { b: Block }) {
  const rest = Object.entries(b).filter(([k]) => !['huddle', 'round', 'member', 'feedback'].includes(k))
  return (
    <div className="space-y-2">
      {rest.map(([k, v]) => (
        <Part key={k} label={k.replace(/_/g, ' ')}>
          {Array.isArray(v) ? <Bullets items={v} /> : <p className="whitespace-pre-wrap">{typeof v === 'object' ? JSON.stringify(v, null, 2) : str(v)}</p>}
        </Part>
      ))}
    </div>
  )
}

function InboundBubble({ reply, member, hueOf }: { reply: Reply; member: string; hueOf: (m: string) => string }) {
  const p = reply.pitch!
  const whose = p.proposedBy === member ? 'your idea' : 'the idea you lead'
  return (
    <Bubble
      who={reply.member}
      hue={hueOf(reply.member)}
      side="left"
      tint
      data={`inbound-${reply.member}`}
      label={<><span className="font-medium" style={{ color: hueOf(reply.member) }}>{who(reply.member)}</span> on {whose} ‘{p.title}’</>}
    >
      <p className="flex flex-wrap items-center gap-2"><AnswerPill answer={reply.answer} /></p>
      {reply.loose && <p className="mt-1 text-[12px] italic text-muted-foreground">(answered it as “{reply.title}” — it could not see the title)</p>}
      {reply.note && <p className="mt-1 whitespace-pre-wrap">{reply.note}</p>}
    </Bubble>
  )
}

function RoundDivider({ huddle, round }: { huddle: Huddle; round: number }) {
  return (
    <div className="flex items-center gap-3 pt-4">
      <span aria-hidden className="h-px flex-1 bg-border" />
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        {stepHeading(huddle.type, round)}
      </h3>
      <span aria-hidden className="h-px flex-1 bg-border" />
    </div>
  )
}

function RoundMessages({ huddle, member, r, hueOf }: { huddle: Huddle; member: string; r: ThreadRound; hueOf: (m: string) => string }) {
  const leader = huddle.leader
  const hue = hueOf(member)
  const b = r.block
  const feedback = str(b?.feedback)
  const named = (label: ReactNode) => (typeof label === 'string' && (label === member || label === leader) ? who(label) : label)
  const me = (key: string, label: ReactNode, body: ReactNode, clamp = true) => (
    <Bubble key={key} who={member} hue={hue} side="right" label={named(label)} data={key} clamp={clamp}>{body}</Bubble>
  )
  // The leader's questions are the point of the thread: never clipped.
  const ada = (key: string, label: ReactNode, body: ReactNode, clamp = false) => (
    <Bubble key={key} who={leader} hue={LEADER_HUE} side="left" label={named(label)} data={key} clamp={clamp}>{body}</Bubble>
  )
  const full = <FullMessage prompt={r.cell?.prompt ?? ''} leader={leader} />
  const out: ReactNode[] = []

  if (huddle.type !== 'work' || r.round > 4 || !b) {
    out.push(ada(`leader-${r.round}`, leader, <><p>{r.ask}</p>{full}</>))
    out.push(me(`${member}-${r.round}`, member, b ? <GenericBody b={b} /> : <NoReply cell={r.cell} member={member} />))
    out.push(<FeedbackLine key="fb" member={member} text={feedback} />)
    return <>{out}</>
  }

  if (r.round === 1) {
    out.push(ada('leader-1', leader, <><p>{r.ask}</p>{full}</>))
    const parts = REPORT_PARTS.filter(([k]) => list(b[k]).length > 0)
    parts.forEach(([k, label], i) =>
      out.push(me(`${member}-1-${k}`, i === 0 ? member : '', <Part label={label}><ReportPart k={k} b={b} /></Part>)),
    )
    if (!parts.length) out.push(me(`${member}-1`, member, <GenericBody b={b} />))
  }

  if (r.round === 2) {
    out.push(ada('leader-2', leader, (
      <>
        <p>{r.ask}</p>
        {r.questions.length > 0 && (
          <>
            <p className="mt-3 font-medium">My questions for you:</p>
            <ol className="mt-1 list-decimal space-y-1.5 pl-5">{r.questions.map((q, i) => <li key={i}>{q}</li>)}</ol>
          </>
        )}
        {full}
      </>
    )))
    const { paired, rows } = pairQA(leaderAsks(r.cell?.prompt ?? '', leader), b)
    rows.forEach((row, i) =>
      out.push(me(`${member}-2-qa-${i}`, i === 0 ? `${who(member)} answers ${who(leader)}` : '', (
        <div data-qa-row>
          {paired ? (
            <blockquote className="mb-1.5 border-l-2 border-border pl-2 text-[12px] leading-snug text-muted-foreground">
              {who(leader)} asked: {row.question}
            </blockquote>
          ) : row.title ? <p className="mb-0.5 text-[13px] font-medium text-foreground-secondary">{row.title}</p> : null}
          <p className="whitespace-pre-wrap">{row.answer}</p>
        </div>
      ))),
    )
    const props = list<RawProposal>(b.proposals)
    props.forEach((p, i) =>
      out.push(me(`${member}-2-proposal-${i}`, `${who(member)} suggests${props.length > 1 ? ` (${i + 1} of ${props.length})` : ''}`, <PitchCard p={p} proposedBy={member} hueOf={hueOf} />, false)),
    )
    if (!props.length) out.push(me(`${member}-2-none`, member, <p className="italic text-muted-foreground">No ideas at this step.</p>))
  }

  if (r.round === 3) {
    out.push(ada('leader-3', leader, (
      <>
        <p>{r.ask}</p>
        {r.joint.length > 0 && (
          <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[13px]">
            {r.joint.map((j) => <li key={j.title}>{j.title} <span className="text-muted-foreground">(led by {who(j.lead)})</span></li>)}
          </ul>
        )}
        {full}
      </>
    )))
    const answers = list<Record<string, unknown>>(b.answers)
    const used = new Set<number>()
    r.joint.forEach((j, n) => {
      out.push(ada(`leader-3-critique-${n}`, `${who(leader)} on ‘${j.title}’`, j.critique
        ? <p className="whitespace-pre-wrap">{j.critique}</p>
        : <p className="italic text-muted-foreground">No comments — just say whether you're in.</p>))
      const i = answers.findIndex((a, k) => !used.has(k) && normTitle(a.title) === normTitle(j.title))
      if (i >= 0) {
        used.add(i)
        out.push(me(`${member}-3-answer-${i}`, member, <AnswerBody a={answers[i]} />))
      }
    })
    answers.forEach((a, i) => {
      if (!used.has(i)) out.push(me(`${member}-3-answer-${i}`, member, <AnswerBody a={a} />))
    })
    if (!r.joint.length && !answers.length) out.push(me(`${member}-3-none`, member, <p className="italic text-muted-foreground">No shared ideas to answer.</p>))
    const revised = list<RawProposal>(b.proposals)
    revised.forEach((p, i) => out.push(me(`${member}-3-revised-${i}`, `${who(member)} updates the idea`, <PitchCard p={p} proposedBy={member} hueOf={hueOf} />, false)))
    if (r.ownQuestions.length) {
      out.push(ada('leader-3-own', `${who(leader)} on your own ideas`, (
        <ol className="list-decimal space-y-1.5 pl-5">{r.ownQuestions.map((q, i) => <li key={i}>{q}</li>)}</ol>
      )))
      // The free text is where a member answers those, so it reads as a reply.
      if (feedback) out.push(me(`${member}-3-also`, member, <p className="whitespace-pre-wrap">{feedback}</p>))
    }
    r.inbound.forEach((x, i) => out.push(<InboundBubble key={`in-${i}`} reply={x} member={member} hueOf={hueOf} />))
    if (!r.ownQuestions.length) out.push(<FeedbackLine key="fb" member={member} text={feedback} />)
    return <>{out}</>
  }

  if (r.round === 4) {
    out.push(ada('leader-4', leader, <><p>{r.ask}</p>{full}</>))
    const rows = list<Record<string, unknown>>(b.resolutions)
    rows.forEach((x, i) => {
      const v = normResolution(x.resolution)
      out.push(me(`${member}-4-${i}`, member, (
        <div data-resolution={v ?? 'unknown'}>
          <p className="flex flex-wrap items-center gap-2"><ResolutionPill v={v} /><span className="font-medium">{str(x.title)}</span></p>
          {str(x.note) && <p className="mt-1 whitespace-pre-wrap">{str(x.note)}</p>}
        </div>
      )))
    })
    if (!rows.length) out.push(me(`${member}-4-none`, member, <p className="italic text-muted-foreground">No changes to settle.</p>))
  }

  out.push(<FeedbackLine key="fb" member={member} text={feedback} />)
  return <>{out}</>
}

function ByAgent({ huddle, member, hueOf }: { huddle: Huddle; member: string; hueOf: (m: string) => string }) {
  const rounds = roundsToShow(huddle).filter((r) => huddle.cells.some((c) => c.round === r))
  const thread = threadFor(huddle, member, rounds)
  return (
    <div data-thread={member} className="space-y-4">
      {thread.map((r) => (
        <section key={r.round} data-round={r.round} aria-label={`Step ${r.round}`} className="space-y-3">
          <RoundDivider huddle={huddle} round={r.round} />
          {r.cell
            ? <RoundMessages huddle={huddle} member={member} r={r} hueOf={hueOf} />
            : <p className="text-center text-[13px] italic text-muted-foreground">{who(member)} had nothing to answer at this step.</p>}
        </section>
      ))}
    </div>
  )
}

// ── By idea ──────────────────────────────────────────────────────────────────

function OutcomeFooter({ t, leader, agents, hueOf }: { t: ProposalThread; leader: string; agents: string[]; hueOf: (m: string) => string }) {
  const o = t.outcome
  if (t.tasks.length) {
    return (
      <div data-outcome-footer="filed" className="rounded-xl border border-success/30 bg-success/10 px-4 py-2.5 text-[13px]">
        <p className="font-semibold text-success">{VERDICT_WORDS.filed}</p>
        <ul className="mt-1 space-y-1">
          {t.tasks.map((task) => {
            const step = nextStep(task, agents)
            return (
              <li key={`${task.agent}-${task.task_id}`} data-task={task.ext_id}>
                <Link to={task.url} className="font-medium underline-offset-2 hover:underline" style={{ color: hueOf(task.agent) }}>
                  {boardLinkText(task.agent)}
                </Link>
                <span className="text-muted-foreground"> · {taskStatusPlain(task.status)}</span>
                {step.text && (
                  <span data-next-step className={step.stuck ? 'text-warning' : 'text-foreground-secondary'}>
                    {' — '}{step.stuck ? <span className="font-semibold">Stuck: </span> : 'next: '}{step.text}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      </div>
    )
  }
  if (o.verdict === 'filed') {
    return <p data-outcome-footer="agreed" className="rounded-xl border border-success/30 bg-success/10 px-4 py-2.5 text-[13px]"><span className="font-semibold text-success">Agreed</span> — not on the boards yet.</p>
  }
  const { why, clear } = holdWords(o, leader)
  return (
    <p data-outcome-footer={o.verdict} className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-2.5 text-[13px]">
      <span className="font-semibold text-warning">{VERDICT_WORDS[o.verdict]}:</span> {why} {clear && <span className="text-muted-foreground">What would un-park it: {clear}</span>}
    </p>
  )
}

const VERDICT_PILL: Record<string, string> = { filed: 'sent to you', held: 'parked', open: 'still being worked out' }

function ProposalThreadView({ huddle, t, hueOf }: { huddle: Huddle; t: ProposalThread; hueOf: (m: string) => string }) {
  const o = t.outcome
  const by = t.pitch?.proposedBy ?? o.proposedBy
  const L = who(huddle.leader)
  return (
    <article data-proposal-thread={o.title} className="space-y-4 rounded-2xl border border-border bg-card/40 p-4 sm:p-5">
      <header>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h3 className="text-[16px] font-semibold leading-snug text-foreground">{o.title}</h3>
          <span className={'inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11px] font-medium ' + (o.verdict === 'filed' ? 'border-success/30 bg-success/10 text-success' : 'border-warning/30 bg-warning/10 text-warning')}>
            {VERDICT_PILL[o.verdict]}
          </span>
        </div>
        <p className="mt-0.5 text-[12px] text-muted-foreground">
          {by !== o.lead ? <>suggested by {who(by)}, with {who(o.lead)} leading</> : <>suggested by {who(o.lead)}</>}
          {o.partners.length ? <> · together with {andList(o.partners)}</> : <> · on its own</>}
        </p>
      </header>
      <Bubble who={by} hue={hueOf(by)} side="right" label={`${possessive(by)} idea · step 2`} data="pitch" clamp={false}>
        {t.pitch ? <PitchCard p={t.pitch.raw} proposedBy={by} hueOf={hueOf} showTitle={false} /> : <p className="italic text-muted-foreground">The idea is not visible to you.</p>}
      </Bubble>
      {t.critique ? (
        <Bubble who={huddle.leader} hue={LEADER_HUE} side="left" label={`${L}'s take · step 3`} data="critique">
          <p className="whitespace-pre-wrap">{t.critique}</p>
        </Bubble>
      ) : o.partners.length === 0 ? (
        <p className="text-center text-[12px] italic text-muted-foreground">On its own — no teammates to ask, so no step 3.</p>
      ) : null}
      {t.replies.map((r, i) => (
        <Bubble
          key={i}
          who={r.member}
          hue={hueOf(r.member)}
          side="left"
          tint
          data={`reply-${r.member}`}
          label={<><span className="font-medium" style={{ color: hueOf(r.member) }}>{who(r.member)}</span>{r.member === o.lead && by !== o.lead ? ' (asked to lead)' : ''} · step 3</>}
        >
          <p className="flex flex-wrap items-center gap-2"><AnswerPill answer={r.answer} /></p>
          {r.loose && <p className="mt-1 text-[12px] italic text-muted-foreground">(answered it as “{r.title}” — it could not see the title)</p>}
          {r.note && <p className="mt-1 whitespace-pre-wrap">{r.note}</p>}
        </Bubble>
      ))}
      {t.silent.length > 0 && (
        <p className="text-center text-[12px] italic text-muted-foreground">{andList(t.silent)} never answered.</p>
      )}
      {t.resolutions.map((r, i) => (
        <Bubble key={i} who={r.member} hue={hueOf(r.member)} side="right" label={`${who(r.member)} on the changes · step 4`} data="resolution">
          <p className="flex flex-wrap items-center gap-2"><ResolutionPill v={r.verdict} /></p>
          {r.note && <p className="mt-1 whitespace-pre-wrap">{r.note}</p>}
        </Bubble>
      ))}
      <OutcomeFooter t={t} leader={huddle.leader} agents={columns(huddle)} hueOf={hueOf} />
    </article>
  )
}

function ByProposal({ huddle, hueOf }: { huddle: Huddle; hueOf: (m: string) => string }) {
  const threads = proposalThreads(huddle)
  if (!threads.length) return <p className="text-[13px] italic text-muted-foreground">No ideas yet.</p>
  return <div className="space-y-6">{threads.map((t) => <ProposalThreadView key={t.outcome.key} huddle={huddle} t={t} hueOf={hueOf} />)}</div>
}

// ── the switcher ─────────────────────────────────────────────────────────────

function Segmented<T extends string>({ label, options, value, onPick, small = false }: {
  label: string; options: { id: T; label: string }[]; value: T; onPick: (v: T) => void; small?: boolean
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-lg border border-border bg-card p-0.5">
      {options.map((v) => (
        <button
          key={v.id}
          type="button"
          aria-pressed={value === v.id}
          onClick={() => onPick(v.id)}
          className={
            'rounded-md font-medium transition-colors ' + (small ? 'px-2.5 py-1 text-[12px] ' : 'px-3 py-1.5 text-[13px] ') +
            (value === v.id ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')
          }
        >
          {v.label}
        </button>
      ))}
    </div>
  )
}

export function HuddleConversation({ huddle }: { huddle: Huddle }) {
  const [params, setParams] = useSearchParams()
  const members = columns(huddle)
  const hueOf = (m: string) => (m === huddle.leader ? LEADER_HUE : memberHue(Math.max(0, members.indexOf(m))))
  const view = readView(params.get('view'))
  const top = view === 'agent' || view === 'proposal' ? 'full' : view
  const want = params.get('with') ?? ''
  const member = members.includes(want) ? want : members[0] ?? ''
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params)
    if (k === 'view' && v === 'story') next.delete('view')
    else next.set(k, v)
    setParams(next, { replace: true })
  }

  return (
    <div data-view={view}>
      <div className="mx-auto mb-6 flex max-w-[760px] flex-wrap items-center gap-3">
        <Segmented
          label="How to read this huddle"
          options={VIEWS}
          value={top}
          onPick={(v) => set('view', v === 'full' ? (view === 'proposal' ? 'proposal' : 'agent') : v)}
        />
        {top === 'full' && (
          <Segmented small label="Read the conversation" options={FULL_VIEWS} value={view as 'agent' | 'proposal'} onPick={(v) => set('view', v)} />
        )}
      </div>

      {view === 'story' && <HuddleStory huddle={huddle} />}

      {view === 'agent' && (
        <div className="mx-auto max-w-[720px]">
          <div role="group" aria-label="Whose conversation with the leader" className="sticky top-0 z-10 -mx-1 mb-2 flex flex-wrap items-center gap-2 bg-background/90 px-1 py-2 backdrop-blur">
            <span className="text-[12px] text-muted-foreground">{who(huddle.leader)} with</span>
            {members.map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={m === member}
                onClick={() => set('with', m)}
                className={
                  'inline-flex items-center gap-1.5 rounded-full border py-0.5 pl-0.5 pr-3 text-[13px] font-medium transition-colors ' +
                  (m === member ? 'text-foreground' : 'border-border text-muted-foreground hover:text-foreground')
                }
                style={m === member ? { borderColor: hueOf(m), background: `color-mix(in oklch, ${hueOf(m)} 12%, transparent)` } : undefined}
              >
                <MemberAvatar slug={m} hue={hueOf(m)} size="sm" />
                {who(m)}
              </button>
            ))}
          </div>
          {member ? <ByAgent huddle={huddle} member={member} hueOf={hueOf} /> : <p className="text-[13px] italic text-muted-foreground">No members yet.</p>}
        </div>
      )}
      {view === 'proposal' && (
        <div className="mx-auto max-w-[760px]"><ByProposal huddle={huddle} hueOf={hueOf} /></div>
      )}
      {(view === 'agent' || view === 'proposal') && huddle.summary && (
        <details data-close className="mx-auto mt-8 max-w-[760px] text-[13px]">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            The email {who(huddle.leader)} sent you, word for word
          </summary>
          <div className="mt-2 whitespace-pre-wrap break-words rounded-xl border border-border bg-card p-4 leading-relaxed text-foreground-secondary">
            <Linkified text={huddle.summary} />
          </div>
        </details>
      )}
      {view === 'map' && <HuddleSequence huddle={huddle} />}
    </div>
  )
}
