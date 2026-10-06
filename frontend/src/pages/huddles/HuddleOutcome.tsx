import { Link } from 'react-router-dom'
import type { Huddle, HuddleOutput } from '@/api/huddles'
import { MemberAvatar } from './HuddleGrid'
import {
  columns, holdWords, memberHue, outcomeOf, sizeWords, taskStatusWords,
  type ProposalOutcome,
} from './huddleModel'

/**
 * The huddle's outcome, first: what was decided and what is waiting on the
 * reader. One card per filed proposal with the board tasks it became grouped
 * beneath it (a joint proposal puts one task on each agent's board, which is
 * why N proposals can mean more than N tasks), then the held ones with why
 * and what would clear them. The conversation that got here sits below,
 * collapsed — this is the part someone arriving from the email reads.
 */

const TASK_TONE: Record<string, string> = {
  suggested: 'bg-special/10 text-special border-special/30',
  in_progress: 'bg-info/10 text-info border-info/30',
  done: 'bg-success/10 text-success border-success/30',
  declined: 'bg-destructive/10 text-destructive border-destructive/30',
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

function People({ p, hueOf }: { p: ProposalOutcome; hueOf: (m: string) => string }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[13px] text-foreground-secondary">
      <MemberAvatar slug={p.lead} hue={hueOf(p.lead)} size="sm" />
      <span>
        <span className="font-medium text-foreground">{p.lead}</span> leads
        {p.partners.length > 0 ? <> · with {p.partners.join(', ')}</> : <> · solo</>}
      </span>
    </span>
  )
}

function Serves({ p }: { p: ProposalOutcome }) {
  if (!p.priority && !p.project) return null
  return (
    <dl className="mt-3 space-y-1.5 text-[13px] leading-snug">
      {p.priority && (
        <div>
          <dt className="inline font-medium text-foreground-secondary">Serves: </dt>
          <dd className="inline text-foreground">{p.priority}</dd>
        </div>
      )}
      {p.project && (
        <div>
          <dt className="inline font-medium text-foreground-secondary">Project: </dt>
          <dd className="inline text-muted-foreground">{p.project}</dd>
        </div>
      )}
    </dl>
  )
}

function TaskRow({ o, lead, hueOf }: { o: HuddleOutput; lead: string; hueOf: (m: string) => string }) {
  return (
    <li data-task={o.ext_id} className="flex items-start gap-2.5 py-2">
      <MemberAvatar slug={o.agent} hue={hueOf(o.agent)} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
          <span className="whitespace-nowrap text-[13px] text-foreground">
            <span className="font-medium">{o.agent}</span>
            <span className="text-muted-foreground">{o.agent === lead ? ' — leads it' : "'s part"}</span>
          </span>
          <Link to={o.url} className="whitespace-nowrap text-[12px] text-primary underline-offset-2 hover:underline">
            {o.ext_id} on {o.agent}&apos;s board →
          </Link>
        </div>
        <span className={`mt-1 inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium ${TASK_TONE[o.status] ?? 'border-border bg-muted text-muted-foreground'}`}>
          {taskStatusWords(o)}
        </span>
      </div>
    </li>
  )
}

function FiledCard({ p, hueOf }: { p: ProposalOutcome; hueOf: (m: string) => string }) {
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
          <>
            <p className="pt-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              {p.tasks.length === 1 ? 'Board task' : `${p.tasks.length} board tasks — one per agent`}
            </p>
            <ul className="divide-y divide-border/60">
              {p.tasks.map((o) => <TaskRow key={`${o.agent}-${o.task_id}`} o={o} lead={p.lead} hueOf={hueOf} />)}
            </ul>
          </>
        ) : (
          <p className="py-2 text-[12px] text-muted-foreground">Agreed — not on the board yet; it lands there when the huddle is filed.</p>
        )}
      </div>
    </article>
  )
}

function HeldCard({ p, leader, hueOf }: { p: ProposalOutcome; leader: string; hueOf: (m: string) => string }) {
  const { why, clear } = holdWords(p, leader)
  const notes = p.arcs.filter((a) => a.note && p.hold?.who.includes(a.partner))
  return (
    <article data-outcome={p.verdict} className="rounded-xl border border-dashed border-border bg-card/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <h3 className="text-[14px] font-semibold leading-snug text-foreground">{p.title}</h3>
        {sizeWords(p.effort, p.confidence) && <span className="shrink-0 text-[12px] text-muted-foreground">{sizeWords(p.effort, p.confidence)}</span>}
      </div>
      <div className="mt-2"><People p={p} hueOf={hueOf} /></div>
      <dl className="mt-3 space-y-1.5 text-[13px] leading-snug">
        <div>
          <dt className="inline font-medium text-warning">{p.verdict === 'open' ? 'Not settled: ' : 'Why held: '}</dt>
          <dd className="inline text-foreground">{why}</dd>
        </div>
        <div>
          <dt className="inline font-medium text-foreground-secondary">What would clear it: </dt>
          <dd className="inline text-foreground-secondary">{clear}</dd>
        </div>
      </dl>
      {notes.length > 0 && (
        <details className="mt-2 text-[12px]">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            {notes.length === 1 ? `What ${notes[0].partner} asked for` : 'What they asked for'}
          </summary>
          <ul className="mt-1.5 space-y-1.5">
            {notes.map((a) => (
              <li key={a.key} className="leading-snug text-foreground-secondary">
                <span className="font-medium text-foreground">{a.partner}:</span> “{a.note}”
              </li>
            ))}
          </ul>
        </details>
      )}
      <Serves p={p} />
    </article>
  )
}

/** The headline sentence: what was decided, and how proposals map to tasks. */
function headline(filed: ProposalOutcome[], tasks: number, held: number, open: number, finished: boolean): string {
  if (!finished) {
    const parts = [filed.length && `${plural(filed.length, 'proposal')} agreed`, open && `${open} still being decided`].filter(Boolean)
    return parts.length ? `In progress: ${parts.join(', ')}.` : 'In progress — no proposals yet.'
  }
  if (filed.length === 0) return held ? `Nothing filed — ${plural(held, 'proposal')} held.` : 'Nothing was proposed.'
  const joint = filed.filter((p) => p.tasks.length > 1).length
  let s = `${plural(filed.length, 'proposal')} filed as ${plural(tasks, 'board task')}`
  if (joint && tasks > filed.length) s += ` — ${joint === 1 ? 'a joint proposal puts' : 'joint proposals put'} one task on each agent's board`
  s += '.'
  if (held) s += ` ${plural(held, 'proposal')} held.`
  return s
}

export function HuddleOutcome({ huddle }: { huddle: Huddle }) {
  const cols = columns(huddle)
  const hueOf = (slug: string) => memberHue(Math.max(0, cols.indexOf(slug)))
  const { filed, held, open, unmatched } = outcomeOf(huddle)
  const tasks = filed.reduce((n, p) => n + p.tasks.length, 0) + unmatched.length
  const waiting = huddle.outputs.filter((o) => o.status === 'suggested').length

  return (
    <section aria-labelledby="huddle-outcome" className="space-y-5">
      <div>
        <h2 id="huddle-outcome" className="text-base font-semibold text-foreground">What was decided</h2>
        <p className="mt-1 text-[14px] leading-relaxed text-foreground-secondary">
          {headline(filed, tasks, held.length, open.length, huddle.finished)}
        </p>
        {waiting > 0 && (
          <p data-todo className="mt-2 rounded-lg border border-special/30 bg-special/10 px-3 py-2 text-[13px] text-foreground">
            <span className="font-semibold">Your move:</span> {waiting === 1 ? 'one task is' : `${waiting} tasks are`} waiting
            for you to accept or decline on the agents&apos; boards — each is linked below.
          </p>
        )}
      </div>

      {filed.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {huddle.finished ? 'Filed' : 'Agreed'} · {filed.length}
          </h3>
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
            {filed.map((p) => <FiledCard key={p.key} p={p} hueOf={hueOf} />)}
          </div>
        </div>
      )}

      {unmatched.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Other board tasks from this huddle · {unmatched.length}
          </h3>
          <ul className="divide-y divide-border/60 rounded-xl border border-border bg-card px-4">
            {unmatched.map((o) => (
              <li key={`${o.agent}-${o.task_id}`} className="py-1">
                <div className="pt-2 text-[13px] font-medium text-foreground">{o.title}</div>
                <ul><TaskRow o={o} lead={o.agent} hueOf={hueOf} /></ul>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(held.length > 0 || open.length > 0) && (
        <div className="space-y-3">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {held.length > 0 ? `Held — not filed · ${held.length}` : `Still being decided · ${open.length}`}
          </h3>
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
            {[...held, ...open].map((p) => <HeldCard key={p.key} p={p} leader={huddle.leader} hueOf={hueOf} />)}
          </div>
        </div>
      )}

      {huddle.finished && filed.length === 0 && held.length === 0 && huddle.outputs.length === 0 && (
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
