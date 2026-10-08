import type { AgentThread } from '@/api/threads'

/**
 * Two agreement threads on the real work-fleet-20261006 huddle, as the threads
 * API serves them: Echo was in on Eva's IDM-talk idea only with changes, and the
 * two settled it directly (agreed); Ace asked Echo for changes to the PRIDE story
 * and they ran out of messages without agreeing.
 */
const msg = (n: number, speaker: string, at: string, block: Record<string, unknown> | null, status = 'done') => ({
  n, speaker, turn_id: `turn-${speaker}-${n}`, status, created_at: at, finished_at: status === 'done' ? at : null,
  content_hidden: false, prompt: `Thread message ${n} for ${speaker}`, block, reply_source: block ? 'closeout' : 'none', reply_error: '',
})

const IDM = 'IDM talk: live demo from Ace, story slide from Echo'
const PRIDE = 'Take PRIDE cholera story to reviewed draft'

export const agreed: AgentThread = {
  id: 'thr-aaaaaaaaaaaa', kind: 'agreement', purpose: "Settle Echo's changes to Eva's IDM talk idea",
  participants: [{ agent: 'eva', role: 'author' }, { agent: 'echo', role: 'asker' }],
  moderator: 'ada', parent: { huddle: 'work-fleet-20261006', title: IDM, lead: 'eva' },
  context: 'The idea, verbatim.\n\nEcho asked: make the story slide a 60-second clip, not a slide.',
  max_messages: 4, messages_used: 2, deadline_at: '2026-10-06T18:30:00Z', status: 'settled',
  outcome: { result: 'agreed', proposal: { title: IDM, why: 'A clip lands better than a slide', plan: ['Ace demo', 'Echo 60s clip'] },
             why: 'Both agree on a 60-second clip.' },
  created_at: '2026-10-06T17:00:00Z', closed_at: '2026-10-06T17:21:00Z',
  messages: [
    msg(1, 'eva', '2026-10-06T17:08:00Z', { thread: 'thr-aaaaaaaaaaaa', n: 1, from: 'eva', position: 'counter',
      says: 'A clip works for me if it is under a minute. Here is the idea with that change.', proposal: { title: IDM } }),
    msg(2, 'echo', '2026-10-06T17:19:00Z', { thread: 'thr-aaaaaaaaaaaa', n: 2, from: 'echo', position: 'agree',
      says: 'Agreed — under a minute it is.' }),
  ],
} as unknown as AgentThread

export const outOfBudget: AgentThread = {
  id: 'thr-bbbbbbbbbbbb', kind: 'agreement', purpose: "Settle Ace's changes to Echo's PRIDE story",
  participants: [{ agent: 'echo', role: 'author' }, { agent: 'ace', role: 'asker' }],
  moderator: 'ada', parent: { huddle: 'work-fleet-20261006', title: PRIDE, lead: 'echo' },
  context: 'The idea, verbatim.', max_messages: 2, messages_used: 2, deadline_at: '2026-10-06T18:30:00Z',
  status: 'out_of_budget', outcome: { result: 'not_agreed', why: 'ran out of messages' },
  created_at: '2026-10-06T17:01:00Z', closed_at: '2026-10-06T17:30:00Z',
  messages: [
    msg(1, 'echo', '2026-10-06T17:10:00Z', { thread: 'thr-bbbbbbbbbbbb', n: 1, from: 'echo', position: 'question',
      says: 'Which reviewer do you have in mind?' }),
    msg(2, 'ace', '2026-10-06T17:28:00Z', { thread: 'thr-bbbbbbbbbbbb', n: 2, from: 'ace', position: 'counter',
      says: 'A field epidemiologist, and the draft should wait for the new numbers.' }),
  ],
} as unknown as AgentThread

export const threads = [agreed, outOfBudget]
