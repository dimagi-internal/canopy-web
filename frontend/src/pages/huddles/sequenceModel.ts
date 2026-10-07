/**
 * The Diagram as a sequence diagram: who said what to whom, in the order it
 * happened. Pure — the component only lays these messages out.
 *
 * One lane per participant (you, the leader, then each member). Each step is
 * the leader's question going out to everyone it was sent to, then each answer
 * coming back in the order it arrived; the huddle ends with the leader's result
 * going to you. Every label is one plain line — the full text opens on click.
 */
import type { Huddle, HuddleCell } from '@/api/huddles'
import { proposalThreads } from './conversationModel'
import {
  answerLines, cellState, columns, leaderAsks, normResolution, proposalLines, reportSummary, roundsToShow,
  type Answer, type Block, type LeaderAsk,
} from './huddleModel'
import { andList, stepAsk, stepName, trimPriority, VERDICT_WORDS, who } from './plainWords'

export const YOU = 'you'

export type AnswerChip = { answer: Answer; title: string }

export type Message = {
  key: string
  step: number
  kind: 'ask' | 'reply' | 'result'
  from: string
  /** The lanes it reaches — several for the leader's question to everyone. */
  to: string[]
  /** When it was sent (an ask) or arrived (a reply); null while still out. */
  at: string | null
  label: string
  /** Round 3's answers, shown as labelled pills instead of a sentence. */
  chips?: AnswerChip[]
  /** Still waiting for it, or it never came. */
  pending?: 'waiting' | 'failed' | 'no-reply' | 'hidden'
  cell?: HuddleCell
  /** For an ask: the questions put to each member. */
  asks?: { member: string; asks: LeaderAsk[] }[]
}

export type Step = { step: number; title: string; messages: Message[] }

const list = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : [])
const time = (s: string | null | undefined) => (s ? new Date(s).getTime() : Number.POSITIVE_INFINITY)

export function lanesOf(h: Pick<Huddle, 'leader' | 'members' | 'cells'>): string[] {
  return [YOU, h.leader, ...columns(h)]
}

/** One line for what a member's answer DID this step. */
export function replyLabel(block: Block, member: string, round: number): { label: string; chips?: AnswerChip[] } {
  if (round === 1) {
    const top = reportSummary(block).top
    return { label: top ? `Top priority: ${trimPriority(top, 90)}` : 'Sent what it is working on' }
  }
  if (round === 2) {
    const ideas = proposalLines(block, member)
    if (!ideas.length) return { label: 'No ideas this time' }
    const one = (p: (typeof ideas)[number]) =>
      `“${p.title}”${p.lead !== member ? `, led by ${who(p.lead)}` : ''}${p.partners.filter((m) => m !== member).length ? ` with ${andList(p.partners.filter((m) => m !== member))}` : ' on its own'}`
    return { label: ideas.length === 1 ? `Idea: ${one(ideas[0])}` : `${ideas.length} ideas: ${ideas.map((p) => `“${p.title}”`).join(', ')}` }
  }
  if (round === 3) {
    const answers = answerLines(block, member)
    if (!answers.length) return { label: 'Nothing to answer' }
    return { label: '', chips: answers.map((a) => ({ answer: a.answer, title: a.title })) }
  }
  const rs = list<Record<string, unknown>>(block.resolutions)
  return {
    label: rs.map((r) => `${normResolution(r.resolution) === 'accept' ? 'Agreed to the changes' : 'Did not agree'}: “${String(r.title ?? '')}”`).join(' · ') || 'Answered',
  }
}

function askOf(h: Huddle, round: number, cells: HuddleCell[]): Message {
  const asks = cells.map((c) => ({ member: c.member, asks: leaderAsks(c.prompt, h.leader) }))
  const n = asks.reduce((s, a) => s + a.asks.length, 0)
  const ask = stepAsk(h.type || 'work', round)
  const to = cells.map((c) => c.member)
  const everyone = to.length === columns(h).length
  return {
    key: `ask-${round}`,
    step: round,
    kind: 'ask',
    from: h.leader,
    to,
    at: cells.map((c) => c.created_at as string | null).filter(Boolean).sort((a, b) => time(a) - time(b))[0] ?? null,
    label: `${everyone ? 'To everyone' : `To ${andList(to)}`}: ${ask.charAt(0).toUpperCase()}${ask.slice(1)}${n ? ` — plus ${n} question${n === 1 ? '' : 's'} of ${h.leader === 'ada' ? 'her' : 'its'} own` : ''}`,
    asks,
  }
}

function resultOf(h: Huddle, after: string | null): Message | null {
  if (!h.finished) return null
  const threads = proposalThreads(h)
  const sent = threads.filter((t) => t.outcome.verdict === 'filed').length
  const parked = threads.length - sent
  const parts = [
    sent ? `${sent} idea${sent === 1 ? '' : 's'} ${VERDICT_WORDS.filed.toLowerCase()}` : '',
    parked ? `${parked} ${VERDICT_WORDS.held.toLowerCase()}` : '',
  ].filter(Boolean)
  return {
    key: 'result', step: 0, kind: 'result', from: h.leader, to: [YOU], at: after,
    label: parts.length ? parts.join(' · ') : 'Nothing came of it this time',
  }
}

/** The whole huddle as steps of messages, in time order. */
export function sequenceOf(h: Huddle): Step[] {
  const type = h.type || 'work'
  const steps: Step[] = []
  let last: string | null = null
  for (const r of roundsToShow(h)) {
    const cells = h.cells.filter((c) => c.round === r).sort((a, b) => columns(h).indexOf(a.member) - columns(h).indexOf(b.member))
    const messages: Message[] = []
    if (cells.length) {
      messages.push(askOf(h, r, cells))
      const replies = [...cells].sort((a, b) => time(a.finished_at) - time(b.finished_at))
      for (const c of replies) {
        const state = cellState(c)
        const base = { key: `reply-${c.member}-${r}`, step: r, kind: 'reply' as const, from: c.member, to: [h.leader], cell: c }
        if (state === 'replied') {
          messages.push({ ...base, at: c.finished_at ?? null, ...replyLabel(c.block as Block, c.member, r) })
          if (c.finished_at && (!last || time(c.finished_at) > time(last))) last = c.finished_at
        } else {
          const pending = state === 'waiting' ? 'waiting' : state === 'failed' ? 'failed' : state === 'hidden' ? 'hidden' : 'no-reply'
          const label = { waiting: 'Still answering…', failed: "Didn't finish", 'no-reply': 'No answer', hidden: 'Answered (hidden from you)' }[pending]
          messages.push({ ...base, at: c.finished_at ?? null, label, pending })
        }
      }
    }
    steps.push({ step: r, title: stepName(type, r), messages })
  }
  const result = resultOf(h, last)
  if (result) steps.push({ step: 0, title: 'The result', messages: [result] })
  return steps
}
