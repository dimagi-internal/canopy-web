import type { ReactNode } from 'react'
import type { Huddle } from '@/api/huddles'
import { AnswerPill } from './BlockView'
import { MemberAvatar } from './HuddleGrid'
import { TaskLine } from './HuddleOutcome'
import type { Output } from './outcomeSummary'
import { pairQA, pitchesOf, proposalThreads, type ProposalThread } from './conversationModel'
import {
  arcsFor, cellAt, columns, holdWords, ideaHue, ideaLetter, leaderAsks, memberHue, sizeWords, type ArcState, type Block,
} from './huddleModel'
import {
  andList, deJargon, firstSentence, gistOf, possessive, resolutionWords, stepName, trimPriority, VERDICT_WORDS, who,
} from './plainWords'

/**
 * The Story: the huddle told top to bottom for someone seeing one for the
 * first time — three numbered steps, then the result, in plain sentences.
 * Each idea keeps one letter and one colour from step 2 (where it was
 * suggested) through step 3 (who's in) to the result, so a reader can follow
 * one idea down the page. Every line opens to the full words; agent prose is
 * shown with the engine's terms translated (deJargon) — the verbatim record is
 * the Full conversation view.
 */

const LEADER_HUE = 'var(--foreground-secondary)'
const str = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'string' ? v : String(v))
function list<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

// ── pieces ───────────────────────────────────────────────────────────────────

function Step({ n, title, children, last = false, id }: { n: ReactNode; title: string; children: ReactNode; last?: boolean; id?: string }) {
  return (
    <li id={id} data-step={typeof n === 'number' ? n : 'result'} className="relative grid grid-cols-[2rem_minmax(0,1fr)] gap-x-3 pb-10 sm:grid-cols-[2.5rem_minmax(0,1fr)] sm:gap-x-5">
      {!last && <span aria-hidden className="absolute bottom-0 left-4 top-10 w-px bg-border sm:left-5" />}
      <span
        aria-hidden
        className={
          'relative z-10 inline-flex size-8 items-center justify-center rounded-full text-[14px] font-semibold sm:size-10 sm:text-[16px] ' +
          (typeof n === 'number' ? 'bg-primary text-primary-foreground' : 'bg-success text-success-foreground ring-4 ring-success/15')
        }
      >
        {n}
      </span>
      <div className="min-w-0 pt-0.5 sm:pt-1.5">
        <h2 className="text-[18px] font-semibold leading-tight text-foreground sm:text-[20px]">
          {typeof n === 'number' && <span className="sr-only">Step {n}: </span>}
          {title}
        </h2>
        <div className="mt-3 space-y-3">{children}</div>
      </div>
    </li>
  )
}

function Lead({ children }: { children: ReactNode }) {
  return <p className="text-[14px] leading-relaxed text-foreground-secondary">{children}</p>
}

/** "<summary>" that opens to more. */
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

function IdeaBadge({ i }: { i: number }) {
  return (
    <span
      aria-hidden
      className="inline-flex size-6 shrink-0 items-center justify-center rounded-md text-[12px] font-bold text-white"
      style={{ background: ideaHue(i) }}
    >
      {ideaLetter(i)}
    </span>
  )
}

function IdeaTitle({ i, title }: { i: number; title: string }) {
  return (
    <h3 className="flex items-start gap-2 text-[15px] font-semibold leading-snug text-foreground">
      <IdeaBadge i={i} />
      <span className="min-w-0"><span className="sr-only">Idea {ideaLetter(i)}: </span>{title}</span>
    </h3>
  )
}

function IdeaCard({ i, children, data }: { i: number; children: ReactNode; data?: Record<string, string> }) {
  return (
    <article
      {...data}
      className="rounded-xl border border-border bg-card p-4"
      style={{ borderLeftWidth: 4, borderLeftColor: ideaHue(i) }}
    >
      {children}
    </article>
  )
}

function Bullets({ items }: { items: unknown[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5 marker:text-muted-foreground">
      {items.map((it, i) => <li key={i} className="break-words">{deJargon(typeof it === 'object' ? JSON.stringify(it) : str(it))}</li>)}
    </ul>
  )
}

// ── step 1 ───────────────────────────────────────────────────────────────────

const REPORT_PARTS: [string, string][] = [
  ['worked_on', 'What it worked on'], ['priorities', "What it thinks Jonathan's priorities are"],
  ['projects', 'Projects'], ['offers', 'What it can offer the others'], ['needs', 'What it needs'],
]

function FullReport({ b }: { b: Block }) {
  return (
    <div className="space-y-3">
      {REPORT_PARTS.filter(([k]) => list(b[k]).length > 0).map(([k, label]) => (
        <div key={k}>
          <div className="mb-1 text-[12px] font-semibold text-foreground">{label}</div>
          {k === 'projects' ? (
            <ul className="space-y-1">
              {list<unknown>(b.projects).map((raw, i) => {
                const p = (raw && typeof raw === 'object' ? raw : { name: raw }) as Record<string, unknown>
                return (
                  <li key={i}>
                    <span className="font-medium text-foreground">{deJargon(str(p.name))}</span>
                    {str(p.state) && <> — {deJargon(str(p.state))}</>}
                    {str(p.next) && <span className="block text-muted-foreground">Next: {deJargon(str(p.next))}</span>}
                  </li>
                )
              })}
            </ul>
          ) : <Bullets items={list(b[k])} />}
        </div>
      ))}
    </div>
  )
}

function StepReports({ huddle, hueOf }: { huddle: Huddle; hueOf: (m: string) => string }) {
  const members = columns(huddle)
  return (
    <ul className="divide-y divide-border/70 rounded-xl border border-border bg-card">
      {members.map((m) => {
        const c = cellAt(huddle, m, 1)
        const b = c?.block ? (c.block as Block) : null
        const did = b ? gistOf(str(list(b.worked_on)[0])) : ''
        const top = b ? trimPriority(str(list(b.priorities)[0])) : ''
        return (
          <li key={m} data-report={m} className="flex gap-3 p-3 sm:p-4">
            <MemberAvatar slug={m} hue={hueOf(m)} />
            <div className="min-w-0 flex-1 space-y-1.5">
              <div className="text-[14px] font-semibold text-foreground">{who(m)}</div>
              {b ? (
                <>
                  {did && <p className="text-[14px] leading-snug text-foreground">Has been working on: {did}.</p>}
                  {top && <p className="text-[14px] leading-snug text-foreground-secondary">Thinks the top priority is: <span className="text-foreground">{top}</span>.</p>}
                  <More label={`Read ${possessive(m)} full answer`}><FullReport b={b} /></More>
                </>
              ) : (
                <p className="text-[13px] italic text-muted-foreground">
                  {c?.content_hidden ? `${who(m)} answered, but you can't see it.` : c ? `${who(m)} hasn't answered yet.` : `${who(m)} wasn't asked.`}
                </p>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// ── step 2 ───────────────────────────────────────────────────────────────────

function IdeaPlan({ raw }: { raw: Record<string, unknown> }) {
  const asks = raw.ask_of_partners && typeof raw.ask_of_partners === 'object' ? Object.entries(raw.ask_of_partners as Record<string, unknown>) : []
  return (
    <div className="space-y-2">
      {str(raw.why) && <p><span className="font-medium text-foreground">Why: </span>{deJargon(str(raw.why))}</p>}
      {list(raw.plan).length > 0 && (
        <div>
          <div className="font-medium text-foreground">The plan</div>
          <ol className="mt-0.5 list-decimal space-y-0.5 pl-5">{list(raw.plan).map((s, i) => <li key={i}>{deJargon(str(s))}</li>)}</ol>
        </div>
      )}
      {asks.map(([m, ask]) => <p key={m}><span className="font-medium text-foreground">What it needs from {who(m)}: </span>{deJargon(str(ask))}</p>)}
      {str(raw.success_measure) && <p><span className="font-medium text-foreground">Done when: </span>{deJargon(str(raw.success_measure))}</p>}
    </div>
  )
}

function StepIdeas({ huddle, ideas, index }: { huddle: Huddle; ideas: ProposalThread[]; index: (t: ProposalThread) => number }) {
  const shownQA = new Set<string>()
  const byPitchOrder = [...ideas].sort((a, b) => index(a) - index(b))
  return (
    <div className="space-y-3">
      {byPitchOrder.map((t) => {
        const i = index(t)
        const o = t.outcome
        const by = t.pitch?.proposedBy ?? o.proposedBy
        const raw = t.pitch?.raw ?? {}
        const firstOfAuthor = !shownQA.has(by)
        shownQA.add(by)
        const cell = cellAt(huddle, by, 2)
        const qa = firstOfAuthor && cell?.block ? pairQA(leaderAsks(cell.prompt, huddle.leader), cell.block as Block) : null
        const size = sizeWords(o.effort, o.confidence)
        const partners = by === o.lead ? o.partners : o.partners.filter((m) => m !== by)
        return (
          <IdeaCard key={o.key} i={i} data={{ 'data-idea': o.title }}>
            <p className="mb-1.5 text-[12px] text-muted-foreground">{who(by)} suggested:</p>
            <IdeaTitle i={i} title={o.title} />
            <p className="mt-1.5 text-[13px] text-foreground-secondary">
              {by !== o.lead ? <>with {who(o.lead)} leading{o.partners.length ? <>, together with {andList(o.partners)}</> : null}</>
                : partners.length ? <>together with {andList(partners)}</> : <>on its own</>}
              {size && <span className="text-muted-foreground"> · {size}</span>}
            </p>
            {str(raw.why) && <p className="mt-2 text-[14px] leading-snug text-foreground"><span className="font-medium">Why: </span>{firstSentence(str(raw.why))}</p>}
            <div className="mt-2 flex flex-col gap-1.5">
              <More label="The whole idea"><IdeaPlan raw={raw} /></More>
              {qa && qa.rows.length > 0 && (
                <More label={`${possessive(huddle.leader)} questions to ${who(by)} and the answers`}>
                  <ol className="space-y-3">
                    {qa.rows.map((row, k) => (
                      <li key={k} data-qa-pair>
                        {row.question && <p className="text-muted-foreground"><span className="font-medium text-foreground">{who(huddle.leader)} asked: </span>{deJargon(row.question)}</p>}
                        {!row.question && row.title && <p className="font-medium text-foreground">{deJargon(row.title)}</p>}
                        <p className="mt-0.5"><span className="font-medium text-foreground">{who(by)}: </span>{deJargon(row.answer)}</p>
                      </li>
                    ))}
                  </ol>
                </More>
              )}
            </div>
          </IdeaCard>
        )
      })}
    </div>
  )
}

// ── step 3 ───────────────────────────────────────────────────────────────────

function StepWhosIn({ huddle, ideas, index, hueOf }: { huddle: Huddle; ideas: ProposalThread[]; index: (t: ProposalThread) => number; hueOf: (m: string) => string }) {
  const arcs = arcsFor(huddle)
  const sorted = [...ideas].sort((a, b) => index(a) - index(b))
  return (
    <div className="space-y-3">
      {sorted.map((t) => {
        const i = index(t)
        const o = t.outcome
        const stateOf = (m: string): ArcState => o.arcs.find((a) => a.partner === m)?.state
          ?? arcs.find((a) => a.partner === m && a.lead === o.lead && a.title === o.title)?.state ?? 'pending'
        return (
          <IdeaCard key={o.key} i={i} data={{ 'data-whos-in': o.title }}>
            <IdeaTitle i={i} title={o.title} />
            {o.partners.length === 0 ? (
              <p className="mt-2 text-[13px] text-foreground-secondary">No teammates needed — {who(o.lead)} takes this on alone, so there was no one to ask.</p>
            ) : (
              <div className="mt-3 space-y-2.5">
                {t.critique && (
                  <div className="flex gap-2.5">
                    <MemberAvatar slug={huddle.leader} hue={LEADER_HUE} size="sm" />
                    <div className="min-w-0 flex-1 text-[13px] leading-snug">
                      <p className="text-foreground-secondary"><span className="font-semibold text-foreground">{possessive(huddle.leader)} take: </span>{firstSentence(t.critique, 260, 80)}</p>
                      <div className="mt-1"><More label={`All of ${possessive(huddle.leader)} take`}><p className="whitespace-pre-wrap">{deJargon(t.critique)}</p></More></div>
                    </div>
                  </div>
                )}
                <ul className="space-y-2.5">
                  {o.partners.map((m) => {
                    const reply = t.replies.find((r) => r.member === m)
                    const note = reply?.note ?? ''
                    return (
                      <li key={m} data-answer-of={m} className="flex gap-2.5">
                        <MemberAvatar slug={m} hue={hueOf(m)} size="sm" />
                        <div className="min-w-0 flex-1 text-[13px] leading-snug">
                          <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <span className="font-semibold text-foreground">{who(m)}:</span>
                            <AnswerPill answer={stateOf(m)} />
                          </p>
                          {note && <p className="mt-1 text-foreground-secondary">{firstSentence(note)}</p>}
                          {note && note.length > firstSentence(note).length + 5 && (
                            <div className="mt-1"><More label={`All of ${possessive(m)} answer`}><p className="whitespace-pre-wrap">{deJargon(note)}</p></More></div>
                          )}
                        </div>
                      </li>
                    )
                  })}
                </ul>
                {t.resolutions.map((r, k) => r.verdict && (
                  <p key={k} className="text-[13px] text-foreground-secondary">
                    Then {who(r.member)}: <span className="font-medium text-foreground">{resolutionWords(r.verdict)}</span>
                    {r.note && <> — {firstSentence(r.note)}</>}
                  </p>
                ))}
              </div>
            )}
          </IdeaCard>
        )
      })}
    </div>
  )
}

// ── the result ───────────────────────────────────────────────────────────────

function Result({ huddle, ideas, index, hueOf }: { huddle: Huddle; ideas: ProposalThread[]; index: (t: ProposalThread) => number; hueOf: (m: string) => string }) {
  const agents = columns(huddle)
  const sent = ideas.filter((t) => t.outcome.verdict === 'filed').sort((a, b) => index(a) - index(b))
  const parked = ideas.filter((t) => t.outcome.verdict !== 'filed').sort((a, b) => index(a) - index(b))
  return (
    <div className="space-y-6">
      {sent.length > 0 && (
        <section aria-labelledby="result-sent" className="space-y-3">
          <h3 id="result-sent" className="text-[13px] font-semibold uppercase tracking-[0.06em] text-success">
            {huddle.finished ? VERDICT_WORDS.filed : 'Agreed so far'} · {sent.length}
          </h3>
          {sent.map((t) => {
            const o = t.outcome
            return (
              <IdeaCard key={o.key} i={index(t)} data={{ 'data-result': 'sent' }}>
                <IdeaTitle i={index(t)} title={o.title} />
                <p className="mt-1.5 text-[13px] text-foreground-secondary">
                  {o.partners.length ? <>{who(o.lead)} leads it, together with {andList(o.partners)}.</> : <>{who(o.lead)} does it alone.</>}
                </p>
                {t.tasks.length > 0 ? (
                  <ul className="mt-2 divide-y divide-border/60 border-t border-border/60">
                    {t.tasks.map((task) => <TaskLine key={`${task.agent}-${task.task_id}`} o={task as Output} lead={o.lead} agents={agents} hueOf={hueOf} />)}
                  </ul>
                ) : (
                  <p className="mt-2 text-[12px] text-muted-foreground">Agreed — it reaches the agents&apos; boards when the huddle wraps up.</p>
                )}
              </IdeaCard>
            )
          })}
        </section>
      )}
      {parked.length > 0 && (
        <section aria-labelledby="result-parked" className="space-y-3">
          <h3 id="result-parked" className="text-[13px] font-semibold uppercase tracking-[0.06em] text-warning">
            {parked.some((t) => t.outcome.verdict === 'held') ? VERDICT_WORDS.held : VERDICT_WORDS.open} · {parked.length}
          </h3>
          {parked.map((t) => {
            const o = t.outcome
            const { why, clear } = holdWords(o, huddle.leader)
            return (
              <IdeaCard key={o.key} i={index(t)} data={{ 'data-result': 'parked' }}>
                <IdeaTitle i={index(t)} title={o.title} />
                <p className="mt-2 text-[14px] leading-snug text-foreground">{why}</p>
                <p className="mt-1 text-[13px] leading-snug text-foreground-secondary"><span className="font-medium">What would un-park it: </span>{clear}</p>
              </IdeaCard>
            )
          })}
        </section>
      )}
      {sent.length === 0 && parked.length === 0 && (
        <p className="text-[14px] text-muted-foreground">{huddle.finished ? 'Nobody suggested anything this time.' : 'Nothing yet — the agents are still answering.'}</p>
      )}
    </div>
  )
}

// ── the story ────────────────────────────────────────────────────────────────

export function HuddleStory({ huddle }: { huddle: Huddle }) {
  const members = columns(huddle)
  const hueOf = (m: string) => (m === huddle.leader ? LEADER_HUE : memberHue(Math.max(0, members.indexOf(m))))
  const ideas = proposalThreads(huddle)
  const order = pitchesOf(huddle).map((p) => p.key)
  const index = (t: ProposalThread) => {
    const i = order.indexOf(t.outcome.key)
    return i >= 0 ? i : order.length + ideas.indexOf(t)
  }
  const L = who(huddle.leader)
  const has = (r: number) => huddle.cells.some((c) => c.round === r)
  const type = huddle.type || 'work'
  const ran4 = has(4)

  return (
    <ol data-story className="mx-auto max-w-[760px]">
      <Step n={1} title={stepName(type, 1)}>
        <Lead>{L} asked each agent what it has been working on and what it thinks Jonathan&apos;s priorities are.</Lead>
        {has(1) ? <StepReports huddle={huddle} hueOf={hueOf} /> : <p className="text-[13px] italic text-muted-foreground">Not started yet.</p>}
      </Step>
      <Step n={2} title={stepName(type, 2)}>
        <Lead>{L} shared everyone&apos;s answers and asked for ideas — alone or together — plus her questions to each.</Lead>
        {ideas.length ? <StepIdeas huddle={huddle} ideas={ideas} index={index} />
          : <p className="text-[13px] italic text-muted-foreground">{has(2) ? 'Nobody suggested anything.' : 'Not started yet.'}</p>}
      </Step>
      <Step n={3} title={stepName(type, 3)}>
        <Lead>
          {L} sent each idea to the teammates it named, with her take on it, and asked each one: are you in?
          {ran4 && <> Where someone was in only with changes, the idea&apos;s lead then said whether they agree.</>}
        </Lead>
        {has(3) || ideas.some((t) => t.outcome.partners.length === 0)
          ? <StepWhosIn huddle={huddle} ideas={ideas} index={index} hueOf={hueOf} />
          : <p className="text-[13px] italic text-muted-foreground">Not started yet.</p>}
      </Step>
      <Step n="✓" title="The result" last id="huddle-result">
        <Lead>
          {huddle.finished
            ? <>Ideas everyone was in on came to you as tasks on the agents&apos; boards; the rest were parked.</>
            : <>Still going — this fills in as the agents answer.</>}
        </Lead>
        <Result huddle={huddle} ideas={ideas} index={index} hueOf={hueOf} />
      </Step>
    </ol>
  )
}
