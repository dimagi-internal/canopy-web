import { Link } from 'react-router-dom'
import type { Huddle } from '@/api/huddles'
import { MemberAvatar } from './MemberAvatar'
import {
  columns, holdWords, ideaHue, ideaLetter, memberHue, outcomeOf, sizeWords,
  type ProposalOutcome,
} from './huddleModel'
import { pitchesOf } from './conversationModel'
import { boardTally, headline, ideaState, yourMove, type IdeaState, type Output } from './outcomeSummary'
import {
  boardLinkText, deJargon, nextStep, peopleWords, taskStatusPlain, trimPriority, VERDICT_WORDS, who,
} from './plainWords'

/**
 * The huddle's outcome: what was decided and what is waiting on the reader.
 * One card per idea sent to the reader, with the board tasks it became grouped
 * beneath it (a shared idea puts one task on each agent's board, which is why
 * N ideas can mean more than N tasks) — each task's status AND its next step,
 * so a task that reads "in progress" but is stuck says so. Then the parked
 * ones with why and what would un-park them. All in plain words (plainWords).
 */

const TASK_TONE: Record<string, string> = {
  suggested: 'bg-special/10 text-special border-special/30',
  in_progress: 'bg-info/10 text-info border-info/30',
  done: 'bg-success/10 text-success border-success/30',
  declined: 'bg-muted text-muted-foreground border-border',
}


const STATE_TONE: Record<IdeaState['tone'], string> = {
  you: 'border-special/40 bg-special/10 text-special',
  stuck: 'border-warning/40 bg-warning/10 text-warning',
  moving: 'border-info/30 bg-info/10 text-info',
  done: 'border-success/30 bg-success/10 text-success',
  no: 'border-border bg-muted text-muted-foreground',
  parked: 'border-dashed border-border text-muted-foreground',
}

/** Every idea on one line each — its letter and colour (the same as in the
 * Story below), its title, and where it stands now. */
function AtAGlance({ huddle, ideas }: { huddle: Huddle; ideas: ProposalOutcome[] }) {
  const order = pitchesOf(huddle).map((p) => p.key)
  const agents = columns(huddle)
  const idx = (p: ProposalOutcome) => {
    const i = order.indexOf(p.key)
    return i >= 0 ? i : order.length + ideas.indexOf(p)
  }
  const sorted = [...ideas].sort((a, b) => idx(a) - idx(b))
  return (
    <ul data-glance className="divide-y divide-border/60 rounded-xl border border-border bg-card">
      {sorted.map((p) => {
        const st = ideaState(p, agents)
        return (
          <li key={p.key} data-glance-idea={p.title} className="flex items-start gap-2.5 px-3 py-2.5 sm:items-center sm:px-4">
            <span aria-hidden className="mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded text-[11px] font-bold text-white sm:mt-0" style={{ background: ideaHue(idx(p)) }}>
              {ideaLetter(idx(p))}
            </span>
            <span className="min-w-0 flex-1 text-[14px] leading-snug text-foreground">{p.title}</span>
            <span className={`inline-flex h-6 shrink-0 items-center whitespace-nowrap rounded-full border px-2.5 text-[12px] font-medium ${STATE_TONE[st.tone]}`}>{st.text}</span>
          </li>
        )
      })}
    </ul>
  )
}

/** One task: whose part, the link to its board, its status, and its next step
 * — "Stuck: …" in the warning colour when it is not moving on its own. */
export function TaskLine({ o, lead, agents, hueOf }: { o: Output; lead: string; agents: string[]; hueOf: (m: string) => string }) {
  const step = nextStep(o, agents)
  return (
    <li data-task={o.ext_id} data-stuck={step.stuck ? '' : undefined} className="flex items-start gap-2.5 py-2">
      <MemberAvatar slug={o.agent} hue={hueOf(o.agent)} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[13px]">
          <span className="font-medium text-foreground">{o.agent === lead ? `${who(o.agent)} leads` : `${who(o.agent)}'s part`}</span>
          <Link to={o.url} className="text-[12px] text-primary underline-offset-2 hover:underline">{boardLinkText(o.agent)} →</Link>
          <span className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium ${TASK_TONE[o.status] ?? 'border-border bg-muted text-muted-foreground'}`}>
            {taskStatusPlain(o.status)}
          </span>
        </div>
        {step.text && (
          <p data-next-step className={'mt-1 text-[12px] leading-snug ' + (step.stuck ? 'text-warning' : 'text-foreground-secondary')}>
            {step.stuck ? <span className="font-semibold">Stuck: </span> : <span className="font-medium">Next: </span>}
            {step.text}
          </p>
        )}
      </div>
    </li>
  )
}

function People({ p, hueOf }: { p: ProposalOutcome; hueOf: (m: string) => string }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[13px] text-foreground-secondary">
      <MemberAvatar slug={p.lead} hue={hueOf(p.lead)} size="sm" />
      <span>{peopleWords(p.lead, p.partners).replace(/^led by/, 'Led by')}</span>
    </span>
  )
}

function Serves({ p }: { p: ProposalOutcome }) {
  if (!p.priority) return null
  return (
    <p className="mt-3 text-[13px] leading-snug">
      <span className="font-medium text-foreground-secondary">For the priority: </span>
      <span className="text-foreground">{trimPriority(p.priority, 200)}</span>
    </p>
  )
}

function FiledCard({ p, agents, hueOf }: { p: ProposalOutcome; agents: string[]; hueOf: (m: string) => string }) {
  const size = sizeWords(p.effort, p.confidence)
  return (
    <article data-outcome="filed" className="rounded-xl border border-border bg-card p-4" style={{ borderLeftWidth: 3, borderLeftColor: hueOf(p.lead) }}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <h3 className="text-[15px] font-semibold leading-snug text-foreground">{p.title}</h3>
        {size && <span className="shrink-0 text-[12px] text-muted-foreground">{size}</span>}
      </div>
      <div className="mt-2"><People p={p} hueOf={hueOf} /></div>
      <Serves p={p} />
      <div className="mt-3 border-t border-border/70 pt-1">
        {p.tasks.length > 0 ? (
          <ul className="divide-y divide-border/60">
            {p.tasks.map((o) => <TaskLine key={`${o.agent}-${o.task_id}`} o={o} lead={p.lead} agents={agents} hueOf={hueOf} />)}
          </ul>
        ) : (
          <p className="py-2 text-[12px] text-muted-foreground">Agreed — it reaches the agents&apos; boards when the huddle wraps up.</p>
        )}
      </div>
    </article>
  )
}

function HeldCard({ p, leader, hueOf }: { p: ProposalOutcome; leader: string; hueOf: (m: string) => string }) {
  const { why, clear } = holdWords(p, leader)
  const notes = p.arcs.filter((a) => a.note && p.hold?.who.includes(a.partner))
  const size = sizeWords(p.effort, p.confidence)
  return (
    <article data-outcome={p.verdict} className="rounded-xl border border-dashed border-border bg-card/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <h3 className="text-[14px] font-semibold leading-snug text-foreground">{p.title}</h3>
        {size && <span className="shrink-0 text-[12px] text-muted-foreground">{size}</span>}
      </div>
      <div className="mt-2"><People p={p} hueOf={hueOf} /></div>
      <dl className="mt-3 space-y-1.5 text-[13px] leading-snug">
        <div>
          <dt className="inline font-medium text-warning">{p.verdict === 'open' ? 'Not settled yet: ' : "Why it's parked: "}</dt>
          <dd className="inline text-foreground">{why}</dd>
        </div>
        <div>
          <dt className="inline font-medium text-foreground-secondary">What would un-park it: </dt>
          <dd className="inline text-foreground-secondary">{clear}</dd>
        </div>
      </dl>
      {notes.length > 0 && (
        <details className="mt-2 text-[12px]">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            {notes.length === 1 ? `The changes ${who(notes[0].partner)} asked for` : 'The changes they asked for'}
          </summary>
          <ul className="mt-1.5 space-y-1.5">
            {notes.map((a) => (
              <li key={a.key} className="leading-snug text-foreground-secondary">
                <span className="font-medium text-foreground">{who(a.partner)}:</span> “{deJargon(a.note)}”
              </li>
            ))}
          </ul>
        </details>
      )}
      <Serves p={p} />
    </article>
  )
}

export function HuddleOutcome({ huddle, compact = false }: { huddle: Huddle; compact?: boolean }) {
  const cols = columns(huddle)
  const hueOf = (slug: string) => memberHue(Math.max(0, cols.indexOf(slug)))
  const { filed, held, open, unmatched } = outcomeOf(huddle)
  const tasks = filed.reduce((n, p) => n + p.tasks.length, 0) + unmatched.length
  const { waiting, onYou, stuck } = boardTally(huddle)
  const move = yourMove(huddle)

  return (
    <section aria-labelledby="huddle-outcome" className="space-y-5">
      <div>
        <h2 id="huddle-outcome" className="text-base font-semibold text-foreground">What was decided</h2>
        <p className="mt-1 text-[14px] leading-relaxed text-foreground-secondary">
          {headline(filed, tasks, held.length, open.length, huddle.finished)}
        </p>
        {move && (
          <p
            data-todo
            className={
              'mt-2 rounded-lg border px-3 py-2 text-[13px] text-foreground ' +
              (waiting || onYou ? 'border-special/30 bg-special/10' : stuck ? 'border-warning/30 bg-warning/10' : 'border-border bg-card')
            }
          >
            <span className="font-semibold">Your move:</span> {move}
            {compact && (
              <a href="#huddle-result" className="ml-1.5 whitespace-nowrap text-primary underline-offset-2 hover:underline">See the result ↓</a>
            )}
          </p>
        )}
      </div>

      {compact && filed.length + held.length + open.length > 0 && (
        <AtAGlance huddle={huddle} ideas={[...filed, ...held, ...open]} />
      )}

      {!compact && filed.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {huddle.finished ? VERDICT_WORDS.filed : 'Agreed so far'} · {filed.length}
          </h3>
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
            {filed.map((p) => <FiledCard key={p.key} p={p} agents={cols} hueOf={hueOf} />)}
          </div>
        </div>
      )}

      {!compact && unmatched.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Other tasks from this huddle · {unmatched.length}
          </h3>
          <ul className="divide-y divide-border/60 rounded-xl border border-border bg-card px-4">
            {unmatched.map((o) => (
              <li key={`${o.agent}-${o.task_id}`} className="py-1">
                <div className="pt-2 text-[13px] font-medium text-foreground">{o.title}</div>
                <ul><TaskLine o={o} lead={o.agent} agents={cols} hueOf={hueOf} /></ul>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!compact && (held.length > 0 || open.length > 0) && (
        <div className="space-y-3">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {held.length > 0 ? `${VERDICT_WORDS.held} · ${held.length}` : `${VERDICT_WORDS.open} · ${open.length}`}
          </h3>
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
            {[...held, ...open].map((p) => <HeldCard key={p.key} p={p} leader={huddle.leader} hueOf={hueOf} />)}
          </div>
        </div>
      )}

      {!compact && huddle.finished && filed.length === 0 && held.length === 0 && huddle.outputs.length === 0 && (
        <p className="rounded-xl border border-dashed border-border p-4 text-[13px] text-muted-foreground">
          Nothing came out of this huddle.
        </p>
      )}
    </section>
  )
}

/** Plain text with its URLs made into links — for the leader's emailed close. */
export function Linkified({ text }: { text: string }) {
  const parts = text.split(/(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g)
  return (
    <>
      {parts.map((s, i) =>
        i % 2 === 1 ? (
          <a key={i} href={s} className="break-all text-primary underline-offset-2 hover:underline">{s}</a>
        ) : (
          <span key={i}>{s}</span>
        ),
      )}
    </>
  )
}
