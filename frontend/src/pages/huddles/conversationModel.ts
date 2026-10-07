/**
 * The conversation as dialogue: who said what to whom, round by round — the
 * pure half of the "By agent" transcript and the "By proposal" threads. Every
 * word here is read from the derived API (each cell's leader prompt and the
 * member's reply block); nothing is paraphrased except the leader's one-line
 * round ask, which is the template's own ask in plain words.
 */
import type { Huddle, HuddleCell, HuddleOutput } from '@/api/huddles'
import {
  cellAt, columns, leaderAsks, normAnswer, normResolution, outcomeOf,
  type Answer, type Block, type LeaderAsk, type ProposalOutcome, type Resolution,
} from './huddleModel'
import { stepAskSentence } from './plainWords'

const norm = (s: unknown) => String(s ?? '').toLowerCase().replace(/\W+/g, ' ').trim()
const str = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'string' ? v : String(v))
function list<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

// ── which view ───────────────────────────────────────────────────────────────

export type ConversationView = 'story' | 'agent' | 'proposal' | 'map'

/** `?view=`: story (the default) · agent / proposal (the full conversation) · map. */
export function readView(raw: string | null): ConversationView {
  return raw === 'agent' || raw === 'proposal' || raw === 'map' ? raw : 'story'
}

// ── the leader's ask, in plain words ─────────────────────────────────────────


/** The first paragraph of a prompt after its header line — the ask, for a
 * huddle type this page has no plain line for. */
function firstParagraph(prompt: string): string {
  const paras = prompt.split(/\n\s*\n/).map((p) => p.replace(/\s+/g, ' ').trim()).filter(Boolean)
  const body = paras.find((p, i) => i > 0 || !/^huddle\s/i.test(p)) ?? paras[0] ?? ''
  return body.length > 400 ? `${body.slice(0, 400).replace(/\s+\S*$/, '')}…` : body
}

export function plainAsk(type: string, round: number, prompt: string): string {
  return stepAskSentence(type, round) || firstParagraph(prompt)
}

// ── matching answers to proposals ────────────────────────────────────────────

export type RawProposal = Record<string, unknown>

/** A round-2 proposal with its full text, as the proposer wrote it. */
export type Pitch = { key: string; title: string; lead: string; proposedBy: string; partners: string[]; raw: RawProposal }

export function pitchesOf(h: Pick<Huddle, 'cells' | 'members'>): Pitch[] {
  const cols = columns(h)
  const out = new Map<string, Pitch>()
  const cells = h.cells.filter((c) => c.round === 2 && c.block).sort((a, b) => cols.indexOf(a.member) - cols.indexOf(b.member))
  for (const c of cells) {
    for (const p of list<RawProposal>((c.block as Block).proposals)) {
      const title = str(p.title).trim()
      if (!title) continue
      const lead = str(p.lead) || c.member
      const key = `${lead}|${norm(title)}`
      if (out.has(key)) continue
      const partners = [...new Set(list<unknown>(p.with).map(str))].filter((m) => m && m !== lead)
      out.set(key, { key, title, lead, proposedBy: c.member, partners, raw: p })
    }
  }
  return [...out.values()]
}

/** One round-3 answer, attached to the proposal it answers. `loose` when it was
 * matched by who leads it rather than by its title (the member could not see
 * the title and wrote its own — the answer quotes it as `asTitle`). */
export type Reply = {
  member: string
  answer: Answer
  note: string
  title: string
  pitch: Pitch | null
  loose: boolean
}

/** Every round-3 answer, each matched to a proposal: by title (preferring the
 * stated lead); failing that, a member who LEADS exactly one proposal someone
 * else made, and did not answer it by title, is taken to be answering that. */
export function repliesOf(h: Pick<Huddle, 'cells' | 'members'>): Reply[] {
  const pitches = pitchesOf(h)
  const out: Reply[] = []
  for (const c of h.cells) {
    if (c.round !== 3 || !c.block) continue
    const rows = list<Record<string, unknown>>((c.block as Block).answers)
    const exact = rows.map((a) => {
      const same = pitches.filter((p) => norm(p.title) === norm(a.title))
      return same.find((p) => p.lead === str(a.lead)) ?? same[0] ?? null
    })
    const ledForMe = pitches.filter((p) => p.lead === c.member && p.proposedBy !== c.member && !exact.includes(p))
    rows.forEach((a, i) => {
      let pitch = exact[i]
      let loose = false
      if (!pitch && ledForMe.length === 1 && exact.filter((x) => !x).length === 1) {
        pitch = ledForMe[0]
        loose = true
      }
      out.push({ member: c.member, answer: normAnswer(a.answer), note: str(a.note), title: str(a.title), pitch, loose })
    })
  }
  return out
}

// ── the leader's critique of each proposal ───────────────────────────────────

/** The leader's round-3 critique of one proposal, read off whichever partner's
 * round-3 prompt quoted it (every partner gets the same critique). */
export function critiqueOf(h: Pick<Huddle, 'cells' | 'leader'>, pitch: Pick<Pitch, 'title'>): string {
  const seen: string[] = []
  for (const c of h.cells) {
    if (c.round !== 3 || !c.prompt) continue
    for (const a of leaderAsks(c.prompt, h.leader)) {
      if (norm(a.about) === norm(pitch.title) && !seen.includes(a.text)) seen.push(a.text)
    }
  }
  return seen.join('\n\n')
}

/** The joint proposals a round-3 prompt put to a member ("### <title> (lead x)"). */
export function jointProposalsIn(prompt: string): { title: string; lead: string }[] {
  const out: { title: string; lead: string }[] = []
  for (const line of prompt.split('\n')) {
    const m = /^###\s+(.+?)\s+\(lead ([^)]*)\)\s*$/.exec(line.trim())
    if (m) out.push({ title: m[1].trim(), lead: m[2].trim() })
  }
  return out
}

// ── question → answer pairing (round 2) ──────────────────────────────────────

export type QA = { question: string; title: string; answer: string }

/** The leader's round-2 questions with the member's `critique_answers`, paired
 * in order when the counts agree. When they differ, nothing is guessed: every
 * answer comes back with its own title and no question. */
export function pairQA(asks: LeaderAsk[], block: Block | null | undefined): { paired: boolean; rows: QA[] } {
  const answers = list<Record<string, unknown>>(block?.critique_answers).map((a) => ({ title: str(a.title), answer: str(a.answer) }))
  const qs = asks.filter((a) => !a.about)
  if (qs.length && qs.length === answers.length) {
    return { paired: true, rows: answers.map((a, i) => ({ question: qs[i].text, ...a })) }
  }
  return { paired: false, rows: answers.map((a) => ({ question: '', ...a })) }
}

// ── one agent's thread ───────────────────────────────────────────────────────

export type ThreadRound = {
  round: number
  cell: HuddleCell | undefined
  /** The leader's ask in plain words. */
  ask: string
  /** Round 2's questions to the member at large. */
  questions: string[]
  /** Round 3: each joint proposal put to the member, with the leader's critique. */
  joint: { title: string; lead: string; critique: string }[]
  /** Round 3: the leader's questions on the member's own proposals. */
  ownQuestions: string[]
  block: Block | null
  /** Round 3: teammates' answers to proposals this member made or leads. */
  inbound: Reply[]
}

export function threadFor(h: Huddle, member: string, rounds: number[]): ThreadRound[] {
  const replies = repliesOf(h)
  return rounds.map((round) => {
    const cell = cellAt(h, member, round)
    const prompt = cell?.prompt ?? ''
    const asks = leaderAsks(prompt, h.leader)
    const joint = round === 3
      ? jointProposalsIn(prompt).map((j) => ({
          ...j,
          critique: asks.filter((a) => norm(a.about) === norm(j.title)).map((a) => a.text).join('\n\n'),
        }))
      : []
    return {
      round, cell,
      ask: plainAsk(h.type, round, prompt),
      questions: round === 2 ? asks.filter((a) => !a.about).map((a) => a.text) : [],
      joint,
      ownQuestions: round === 3 ? asks.filter((a) => a.about === 'your own proposals').map((a) => a.text) : [],
      block: cell?.block ? (cell.block as Block) : null,
      inbound: round === 3
        ? replies.filter((r) => r.member !== member && r.pitch && (r.pitch.proposedBy === member || r.pitch.lead === member))
        : [],
    }
  })
}

// ── one proposal's thread ────────────────────────────────────────────────────

export type ProposalThread = {
  outcome: ProposalOutcome
  pitch: Pitch | null
  critique: string
  replies: Reply[]
  /** Partners who never answered. */
  silent: string[]
  resolutions: { member: string; verdict: Resolution | null; note: string }[]
  tasks: HuddleOutput[]
}

export function proposalThreads(h: Huddle): ProposalThread[] {
  const outcome = outcomeOf(h)
  const pitches = pitchesOf(h)
  const replies = repliesOf(h)
  const ordered = [...outcome.filed, ...outcome.held, ...outcome.open]
  return ordered.map((o) => {
    const pitch = pitches.find((p) => p.key === o.key) ?? null
    const mine = replies.filter((r) => r.pitch?.key === o.key)
    const resolutions: ProposalThread['resolutions'] = []
    for (const c of h.cells) {
      if (c.round !== 4 || !c.block || c.member !== o.lead) continue
      for (const r of list<Record<string, unknown>>((c.block as Block).resolutions)) {
        if (norm(r.title) === norm(o.title)) resolutions.push({ member: c.member, verdict: normResolution(r.resolution), note: str(r.note) })
      }
    }
    return {
      outcome: o, pitch,
      critique: critiqueOf(h, o),
      replies: mine,
      silent: o.partners.filter((m) => !mine.some((r) => r.member === m)),
      resolutions,
      tasks: o.tasks,
    }
  })
}
