/**
 * The Diagram as a sequence diagram: who said what to whom, in the order it
 * happened. Pure — the component only lays these messages out.
 *
 * One lane per participant (you, the leader, then each member). Each step is
 * the leader's question going out to everyone it was sent to, then each answer
 * coming back in the order it arrived; the huddle ends with the leader's result
 * going to you. Every label is one plain line — the full text opens on click.
 *
 * Where a teammate was in only with changes, the idea's lead and that teammate
 * settle them in a direct conversation (an agreement thread): step 4, "Settling
 * changes", shows each of its messages as a DIRECT arrow from the speaker to the
 * other agent — the leader is not in the middle of it.
 */
import { priorityNumber } from './briefWords'
import type { Huddle, HuddleCell } from '@/api/huddles'
import type { AgentThread, ThreadMessage } from '@/api/threads'
import { audienceOf, messageState, PENDING_WORDS, POSITION_WORDS, said, statusWords } from '../threads/threadModel'
import { proposalThreads } from './conversationModel'
import {
  agreementParties, agreementThreads, answerLines, cellState, columns, leaderAsks, normResolution, proposalLines,
  reportSummary, roundsToShow,
  type Answer, type Block, type LeaderAsk,
} from './huddleModel'
import { andList, firstSentence, stepAsk, stepName, trimPriority, VERDICT_WORDS, who } from './plainWords'

export const YOU = 'you'

/** One answer in step 3, and whose idea it answers (when that is a teammate). */
export type AnswerChip = { answer: Answer; title: string; lead: string }

export type Message = {
  key: string
  step: number
  /** `direct`: one agent speaking straight to another, in an agreement thread;
   * `settled`: how that conversation ended. */
  kind: 'ask' | 'reply' | 'result' | 'direct' | 'settled'
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
  /** For a direct message: its thread and the message itself. */
  thread?: { thread: AgentThread; message?: ThreadMessage; title: string }
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
    // A brief-era report names where it can move the brief's priorities.
    const lever = list<Record<string, unknown>>(block.levers).find((l) => l && typeof l === 'object')
    if (lever) {
      const n = priorityNumber(lever.priority)
      return { label: `Can move ${n === null ? 'a priority' : `priority ${n}`}: ${trimPriority(String(lever.move ?? ''), 90)}` }
    }
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
    return { label: '', chips: answers.map((a) => ({ answer: a.answer, title: a.title, lead: a.lead })) }
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

/** One direct message of an agreement thread, as a Diagram row. */
export function directMessageOf(t: AgentThread, m: ThreadMessage): Message {
  const { title } = agreementParties(t)
  const state = messageState(m)
  const base = {
    key: `thread-${t.id}-${m.n}`, step: 4, kind: 'direct' as const, from: m.speaker,
    to: audienceOf(t, m.speaker), at: (m.finished_at ?? m.created_at ?? null) as string | null,
    thread: { thread: t, message: m, title },
  }
  if (state !== 'replied') return { ...base, label: PENDING_WORDS[state], pending: state }
  const { position, says } = said(m)
  const words = position ? POSITION_WORDS[position] : 'Says'
  return { ...base, label: `${words}${says ? `: ${firstSentence(says, 120, 40)}` : ''}` }
}

/** Step 4 as direct conversations: every agreement thread's messages, each
 * thread closed by a line saying how it ended — all in time order, like every
 * other step (two threads running at once interleave; each arrow still says
 * who is talking to whom). A message still being written sorts last. */
export function settlingMessages(h: Huddle): Message[] {
  const out: Message[] = []
  const threads = [...agreementThreads(h)].sort((a, b) => time(a.created_at) - time(b.created_at))
  for (const t of threads) {
    const { lead, asker, title } = agreementParties(t)
    const msgs = [...(t.messages ?? [])].sort((a, b) => a.n - b.n)
    for (const m of msgs) out.push(directMessageOf(t, m))
    if (t.status !== 'open') {
      out.push({
        // The moderator closes a thread, so the close is the moderator's line to
        // both sides — not either agent's (first live thread, 2026-10-08, read
        // "Eva · Agreed on …").
        key: `thread-${t.id}-end`, step: 4, kind: 'settled', from: t.moderator || h.leader, to: [lead, asker],
        at: (t.closed_at ?? null) as string | null,
        label: `Closed it: ${who(lead)} and ${who(asker)} — ${statusWords(t).charAt(0).toLowerCase()}${statusWords(t).slice(1)} on “${title}”`,
        thread: { thread: t, title },
      })
    }
  }
  return out
    .map((m, i) => ({ m, i }))
    .sort((a, b) => time(a.m.at) - time(b.m.at) || a.i - b.i)
    .map(({ m }) => m)
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
  const direct = settlingMessages(h)
  if (direct.length) {
    const four = steps.find((s) => s.step === 4)
    if (four) four.messages.push(...direct)
    else steps.push({ step: 4, title: stepName(type, 4), messages: direct })
    for (const m of direct) if (m.at && (!last || time(m.at) > time(last))) last = m.at
  }
  const result = resultOf(h, last)
  if (result) steps.push({ step: 0, title: 'The result', messages: [result] })
  return steps
}
