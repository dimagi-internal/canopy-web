import { useState, type CSSProperties } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { Huddle } from '@/api/huddles'
import { POSITION_WORDS, said, threadHref } from '../threads/threadModel'
import { AnswerPill, BlockView } from './BlockView'
import { MemberAvatar } from './MemberAvatar'
import { arcsFor, columns, memberHue, type Block } from './huddleModel'
import { andList, deJargon, possessive, who } from './plainWords'
import { lanesOf, sequenceOf, YOU, type Message } from './sequenceModel'

/**
 * The Diagram: a sequence diagram of the huddle. One vertical line per
 * participant — you, the leader, each agent — and time running down. Every
 * message is one row: an arrow from who sent it to who it reached, its time,
 * and one plain line saying what it was. A solid arrow is the leader asking, a
 * dashed one an answer coming back, and the last arrow is the result coming to
 * you. Click a row for the full text.
 *
 * Jonathan, 2026-10-07: "we mostly want to show the time sequence of the
 * flow" — so the only thing position encodes is who and when, and the only
 * colour is each participant's own.
 *
 * Step 4 ("Settling changes") is different in kind: the idea's lead and the
 * teammate who asked for changes talk DIRECTLY, so its arrows run agent to agent
 * (a bold line in the info colour), not through the leader.
 */

const LANE = '4.75rem'
const TIME = '4.5rem'

const LINE = {
  ask: { color: 'var(--foreground-secondary)', dash: '', width: 1.5 },
  reply: { color: 'var(--muted-foreground)', dash: '4 3', width: 1.5 },
  result: { color: 'var(--primary)', dash: '', width: 2.5 },
  direct: { color: 'var(--info)', dash: '', width: 2 },
  settled: { color: 'var(--success)', dash: '', width: 2.5 },
} as const

function clock(iso: string | null): string {
  return iso ? new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : ''
}

/** The step's first time, when it falls on a different day from the step
 * before (step 4 can run a day or more after step 3) — the rows only show a
 * clock time, so the day goes on the step's band. */
function newDay(steps: { messages: { at: string | null }[] }[], i: number): string | null {
  const first = (k: number) => steps[k]?.messages.find((m) => m.at)?.at ?? null
  const here = first(i)
  if (!here || i === 0) return null
  let k = i - 1
  while (k >= 0 && !first(k)) k--
  const before = k >= 0 ? first(k) : null
  return before && day(before) !== day(here) ? here : null
}

function day(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) : ''
}

/** The arrow for one message, across the lane columns. Lane centres are at
 * (i + ½) / n of the width, so everything is a percentage — no measuring. */
function Arrow({ m, lanes }: { m: Message; lanes: string[] }) {
  const n = lanes.length
  const x = (lane: string) => ((lanes.indexOf(lane) + 0.5) / n) * 100
  const from = x(m.from)
  const ends = m.to.filter((t) => lanes.includes(t)).map(x)
  if (!ends.length) return null
  const far = ends.reduce((a, b) => (Math.abs(b - from) > Math.abs(a - from) ? b : a))
  const s = LINE[m.kind]
  return (
    <svg aria-hidden data-arrow={m.kind} className="absolute inset-0 h-full w-full overflow-visible" preserveAspectRatio="none">
      <line x1={`${from}%`} x2={`${far}%`} y1="50%" y2="50%" stroke={s.color} strokeWidth={s.width}
        strokeDasharray={m.pending ? '2 4' : s.dash || undefined} vectorEffect="non-scaling-stroke" />
      <circle cx={`${from}%`} cy="50%" r={3.5} fill={s.color} />
      {ends.map((e) => {
        const dir = e > from ? 1 : -1
        return (
          <svg key={e} x={`${e}%`} y="50%" overflow="visible">
            <path d={`M ${-dir * 8} -4.5 L 0 0 L ${-dir * 8} 4.5 z`} fill={s.color} />
          </svg>
        )
      })}
    </svg>
  )
}

function Lifelines({ count }: { count: number }) {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-y-0 hidden md:block" style={{ left: TIME, width: `calc(${LANE} * ${count})` }}>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="absolute inset-y-0 w-px bg-border" style={{ left: `calc(${LANE} * ${i} + ${LANE} / 2)` }} />
      ))}
    </div>
  )
}

function Said({ m, leader, lanes }: { m: Message; leader: string; lanes: string[] }) {
  const to = m.kind === 'result' ? 'you' : m.kind === 'reply' ? who(leader) : m.kind === 'direct' ? andList(m.to) : null
  return (
    <span className="min-w-0 text-[13px] leading-snug">
      <span className="font-semibold text-foreground">{who(m.from)}</span>
      {to && <span className="text-muted-foreground"> → {to}</span>}
      <span className="text-muted-foreground"> · </span>
      {m.chips ? (
        <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 align-middle">
          {m.chips.map((c) => (
            <span key={c.title} className="inline-flex min-w-0 items-center gap-1.5">
              <AnswerPill answer={c.answer} />
              <span className="text-foreground-secondary">
                {lanes.includes(c.lead) && c.lead !== m.from && <span className="text-muted-foreground">on {possessive(c.lead)} idea </span>}
                “{c.title}”
              </span>
            </span>
          ))}
        </span>
      ) : (
        <span className={m.pending ? 'italic text-muted-foreground' : m.kind === 'result' || m.kind === 'settled' ? 'font-medium text-foreground' : 'text-foreground-secondary'}>
          {m.label}
        </span>
      )}
    </span>
  )
}

function ThreadDetail({ m }: { m: Message }) {
  const { workspace = '' } = useParams()
  const t = m.thread!
  const msg = t.message
  const link = (
    <Link className="text-primary underline-offset-2 hover:underline" to={threadHref(workspace, t.thread.id)}>
      Open the whole conversation
    </Link>
  )
  if (!msg || !msg.block) {
    const why = String((t.thread.outcome as Record<string, unknown>)?.why ?? '')
    return (
      <div className="space-y-1.5 text-[12px] text-foreground-secondary">
        {m.kind === 'settled'
          ? <p>{m.label}.{why && <> {deJargon(why)}</>}</p>
          : <p className="text-muted-foreground">{m.pending === 'hidden'
            ? "You can see that it was sent, not what it said — a turn's content is for the people who run the agent."
            : m.pending === 'waiting' ? `${who(m.from)} is still writing.` : 'No message came back that the page could read.'}</p>}
        {msg?.reply_error && <p className="font-mono text-[11px] text-warning">{msg.reply_error}</p>}
        <p>{link}</p>
      </div>
    )
  }
  const { position, says, proposal } = said(msg)
  return (
    <div className="space-y-2 text-[12px] leading-snug text-foreground-secondary">
      <p className="text-muted-foreground">
        {who(m.from)} to {andList(m.to)}, about “{t.title}”{position ? <> · <span className="font-medium text-foreground">{POSITION_WORDS[position]}</span></> : null}
      </p>
      {says && <p className="whitespace-pre-wrap text-foreground">{deJargon(says)}</p>}
      {proposal && <p><span className="font-medium text-foreground">Offers a revised idea: </span>{String(proposal.title ?? '')}</p>}
      <p>{link}</p>
    </div>
  )
}

function Detail({ m, huddle }: { m: Message; huddle: Huddle }) {
  if (m.kind === 'direct' || m.kind === 'settled') return <ThreadDetail m={m} />
  if (m.kind === 'ask') {
    const any = m.asks?.some((a) => a.asks.length)
    return (
      <div className="space-y-2">
        {!any && <p className="text-[12px] text-muted-foreground">The same question to everyone, with nothing extra for anyone.</p>}
        {m.asks?.filter((a) => a.asks.length).map((a) => (
          <section key={a.member}>
            <h4 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">To {who(a.member)}</h4>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-[12px] leading-snug text-foreground-secondary">
              {a.asks.map((q, i) => <li key={i}>{q.about && <span className="text-muted-foreground">on “{q.about}”: </span>}{q.text}</li>)}
            </ul>
          </section>
        ))}
      </div>
    )
  }
  if (m.kind === 'result') {
    return <p className="text-[12px] text-foreground-secondary">Each idea, who does what and where it stands is under <a className="text-primary underline-offset-2 hover:underline" href="#huddle-outcome">What was decided</a> at the top of the page.</p>
  }
  if (m.cell?.block) return <BlockView block={m.cell.block as Block} member={m.from} arcs={arcsFor(huddle)} />
  if (m.pending === 'hidden') return <p className="text-[12px] text-muted-foreground">You can see that it ran, not what it said — a turn&apos;s content is for the people who run the agent.</p>
  if (m.pending === 'waiting') return <p className="text-[12px] text-muted-foreground">{who(m.from)} is still working on its answer.</p>
  return (
    <div className="space-y-1 text-[12px]">
      <p className="text-muted-foreground">{m.pending === 'failed' ? "It didn't finish, so there's no answer." : 'No answer came back that the page could read.'}</p>
      {m.cell?.reply_error && <p className="font-mono text-[11px] text-warning">{m.cell.reply_error}</p>}
    </div>
  )
}

export function HuddleSequence({ huddle }: { huddle: Huddle }) {
  const lanes = lanesOf(huddle)
  const members = columns(huddle)
  const steps = sequenceOf(huddle)
  const [open, setOpen] = useState<string | null>(null)
  const hue = (lane: string) => (lane === YOU ? 'var(--primary)' : lane === huddle.leader ? 'var(--foreground-secondary)' : memberHue(members.indexOf(lane)))
  // Phone: time and words only (no room for lanes); md and up: the lanes too.
  const grid = { '--seq-cols': `${TIME} repeat(${lanes.length}, ${LANE}) minmax(0, 1fr)` } as CSSProperties
  const firstAt = steps[0]?.messages[0]?.at ?? null

  return (
    <div data-sequence className="relative mx-auto max-w-[1200px]">
      <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[12px] text-muted-foreground">
        <span>Time runs down{firstAt ? ` · ${day(firstAt)}` : ''}</span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="26" height="8" aria-hidden><line x1="1" y1="4" x2="25" y2="4" stroke={LINE.ask.color} strokeWidth="1.5" /></svg>
          {who(huddle.leader)} asks
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="26" height="8" aria-hidden><line x1="1" y1="4" x2="25" y2="4" stroke={LINE.reply.color} strokeWidth="1.5" strokeDasharray="4 3" /></svg>
          an answer comes back
        </span>
        {steps.some((s) => s.messages.some((m) => m.kind === 'direct')) && (
          <span className="inline-flex items-center gap-1.5">
            <svg width="26" height="8" aria-hidden><line x1="1" y1="4" x2="25" y2="4" stroke={LINE.direct.color} strokeWidth="2" /></svg>
            two agents talk directly
          </span>
        )}
        <span>Click a row to read it in full.</span>
      </div>

      <div className="relative">
        <Lifelines count={lanes.length} />

        {/* who is who */}
        <div className="sticky top-0 z-20 grid grid-cols-[4.5rem_minmax(0,1fr)] md:[grid-template-columns:var(--seq-cols)] items-end border-b border-border bg-background/95 pb-2 backdrop-blur" style={grid}>
          <span />
          {lanes.map((l) => (
            <div key={l} data-lane={l} className="hidden flex-col items-center gap-1 md:flex">
              <MemberAvatar slug={l === YOU ? 'jonathan' : l} hue={hue(l)} size="sm" />
              <span className="text-[11px] font-medium text-foreground">{l === YOU ? 'You' : who(l)}</span>
            </div>
          ))}
          <span className="col-start-2 text-[11px] font-semibold md:col-start-auto md:pl-4 uppercase tracking-[0.06em] text-muted-foreground">What was said</span>
        </div>

        {steps.map((s, i) => (
          <section key={s.step} data-step={s.step} aria-label={s.title}>
            <div className="relative z-10 mb-1 mt-4 grid grid-cols-[4.5rem_minmax(0,1fr)] md:[grid-template-columns:var(--seq-cols)]" style={grid}>
              <div className="col-span-full rounded-md border border-border/70 bg-muted/90 px-3 py-1.5 text-[12px]">
                <span className="font-semibold text-foreground">{s.step ? `Step ${s.step} · ${s.title}` : s.title}</span>
                {newDay(steps, i) && <span data-step-day className="ml-2 text-muted-foreground">· {day(newDay(steps, i))}</span>}
              </div>
            </div>
            {s.messages.length === 0 && (
              <p className="relative z-10 py-2 pl-[4.5rem] text-[12px] italic text-muted-foreground">Not started yet.</p>
            )}
            {s.messages.map((m) => {
              const isOpen = open === m.key
              return (
                <div key={m.key} data-message={m.key} data-kind={m.kind}>
                  <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : m.key)}
                    className="group grid w-full grid-cols-[4.5rem_minmax(0,1fr)] items-start md:[grid-template-columns:var(--seq-cols)] rounded-md py-1.5 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-primary"
                    style={grid}>
                    <span className="pr-2 text-right text-[11px] leading-5 tabular-nums text-muted-foreground">{clock(m.at)}</span>
                    <span className="relative hidden h-5 md:block" style={{ gridColumn: `2 / span ${lanes.length}` }}>
                      <Arrow m={m} lanes={lanes} />
                    </span>
                    <span className="col-start-2 flex min-w-0 items-start gap-2 pl-0 md:col-start-auto md:pl-4">
                      <Said m={m} leader={huddle.leader} lanes={lanes} />
                      <span aria-hidden className={'ml-auto shrink-0 pt-0.5 text-[10px] text-muted-foreground transition-transform ' + (isOpen ? 'rotate-90' : '')}>▸</span>
                    </span>
                  </button>
                  {isOpen && (
                    <div className="relative z-10 grid grid-cols-[4.5rem_minmax(0,1fr)] pb-2 md:[grid-template-columns:var(--seq-cols)]" style={grid}>
                      <div data-detail className="col-start-2 col-end-[-1] rounded-xl border border-border bg-card p-3 shadow-sm md:col-start-3">
                        <Detail m={m} huddle={huddle} />
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </section>
        ))}
      </div>
    </div>
  )
}
