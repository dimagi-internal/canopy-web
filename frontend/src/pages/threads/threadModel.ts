/**
 * The pure half of the thread page and of the huddle's "Settling changes" step:
 * what each message's position means in plain words, how a thread ended, and who
 * is talking to whom. No React here.
 *
 * A thread is a bounded conversation between named agents (apps/threads). Each
 * message is a turn for its speaker; its words are the speaker's reply block
 * (`says`, `position`, and optionally a revised `proposal`). Nothing here invents
 * state — an unread or unanswered message says so.
 */
import type { AgentThread, ThreadMessage } from '@/api/threads'

export type Position = 'agree' | 'counter' | 'decline' | 'question'

export function normPosition(raw: unknown): Position | null {
  const s = String(raw ?? '').toLowerCase().replace(/[^a-z]/g, '')
  if (s.startsWith('agree')) return 'agree'
  if (s.startsWith('counter')) return 'counter'
  if (s.startsWith('decline')) return 'decline'
  if (s.startsWith('question')) return 'question'
  return null
}

export const POSITION_WORDS: Record<Position, string> = {
  agree: 'Agrees',
  counter: 'Suggests a change',
  decline: "Doesn't agree",
  question: 'Asks',
}

export type Participant = { agent: string; role: string }

export function participantsOf(t: Pick<AgentThread, 'participants'>): Participant[] {
  return (Array.isArray(t.participants) ? t.participants : [])
    .map((p) => ({ agent: String((p as Record<string, unknown>)?.agent ?? ''), role: String((p as Record<string, unknown>)?.role ?? '') }))
    .filter((p) => p.agent)
}

/** Everyone in the thread but the speaker — who a message is addressed to. */
export function audienceOf(t: Pick<AgentThread, 'participants'>, speaker: string): string[] {
  return participantsOf(t).map((p) => p.agent).filter((a) => a !== speaker)
}

/** How a thread ended, for an agreement: agreed, not agreed, or still open. */
export type Result = 'agreed' | 'not_agreed' | null

export function resultOf(t: Pick<AgentThread, 'status' | 'outcome'>): Result {
  if (t.status === 'open') return null
  if (t.status === 'settled' && String((t.outcome as Record<string, unknown>)?.result ?? '') === 'agreed') return 'agreed'
  return 'not_agreed'
}

/** One plain line for where the thread stands. */
export function statusWords(t: Pick<AgentThread, 'status' | 'outcome'>): string {
  switch (t.status) {
    case 'open': return 'Still talking'
    case 'settled': return resultOf(t) === 'agreed' ? 'Agreed' : "Didn't agree"
    case 'out_of_budget': return 'Ran out of messages without agreeing'
    case 'timed_out': return 'Ran out of time without agreeing'
    case 'cancelled': return 'Stopped'
    default: return t.status.replace(/_/g, ' ')
  }
}

export type MessageState = 'replied' | 'hidden' | 'waiting' | 'failed' | 'no-reply'

const PENDING = new Set(['queued', 'claimed', 'running'])
const ENDED_BADLY = new Set(['failed', 'lost', 'error', 'expired', 'cancelled', 'canceled', 'missed'])

export function messageState(m: ThreadMessage): MessageState {
  if (m.content_hidden) return 'hidden'
  if (m.block) return 'replied'
  if (PENDING.has(m.status)) return 'waiting'
  if (ENDED_BADLY.has(m.status)) return 'failed'
  return 'no-reply'
}

export const PENDING_WORDS: Record<Exclude<MessageState, 'replied'>, string> = {
  waiting: 'Still writing…',
  failed: "Didn't finish",
  'no-reply': 'No message came back',
  hidden: 'Said something (hidden from you)',
}

/** The parts of a message as the page shows them. */
export function said(m: ThreadMessage): { position: Position | null; says: string; proposal: Record<string, unknown> | null } {
  const b = (m.block ?? {}) as Record<string, unknown>
  const p = b.proposal
  return {
    position: normPosition(b.position),
    says: String(b.says ?? ''),
    proposal: p && typeof p === 'object' && !Array.isArray(p) && Object.keys(p).length ? (p as Record<string, unknown>) : null,
  }
}

/** "3 of 4 messages used". */
export function budgetWords(t: Pick<AgentThread, 'messages_used' | 'max_messages'>): string {
  return `${t.messages_used} of ${t.max_messages} message${t.max_messages === 1 ? '' : 's'} used`
}

/** The thread page for a thread, under a workspace. */
export function threadHref(workspace: string, id: string): string {
  return `/w/${workspace}/threads/${id}`
}
