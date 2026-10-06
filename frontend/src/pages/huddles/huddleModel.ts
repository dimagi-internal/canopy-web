/**
 * The pure half of the huddle pages: round names, who-answered-what for the
 * co-sign arcs, the leader's critique pulled out of a round prompt, and a stable
 * colour per member. No React here, so all of it is unit-testable.
 *
 * A huddle is DERIVED server-side (apps/huddles): the API hands back one cell per
 * (member, round) with the member's parsed reply block. Nothing below invents
 * state — an arc is drawn only from what blocks actually say.
 */
import type { Huddle, HuddleCell } from '@/api/huddles'

export type Block = Record<string, unknown>

/** Round titles per huddle type. Unknown types just get "Round N". */
const ROUND_NAMES: Record<string, Record<number, string>> = {
  work: { 1: 'Report', 2: 'Roundtable', 3: 'Co-sign' },
}

export function roundName(type: string, round: number): string {
  return ROUND_NAMES[type]?.[round] ?? `Round ${round}`
}

/** Rounds the grid shows: every type round up to the furthest one dispatched,
 * so a work huddle in round 1 still shows what is coming. */
export function roundsToShow(h: Pick<Huddle, 'type' | 'rounds_dispatched' | 'cells'>): number[] {
  const known = Object.keys(ROUND_NAMES[h.type] ?? {}).map(Number)
  const seen = h.cells.map((c) => c.round)
  const top = Math.max(h.rounds_dispatched, ...known, ...seen, 1)
  return Array.from({ length: top }, (_, i) => i + 1)
}

/** Column order: the members as the leader listed them, then anyone who has a
 * cell but was not listed (a late addition), never the leader unless it played. */
export function columns(h: Pick<Huddle, 'members' | 'cells'>): string[] {
  const out = [...h.members]
  for (const c of h.cells) if (!out.includes(c.member)) out.push(c.member)
  return out
}

export function cellAt(h: Pick<Huddle, 'cells'>, member: string, round: number): HuddleCell | undefined {
  return h.cells.find((c) => c.member === member && c.round === round)
}

const PENDING = new Set(['queued', 'claimed', 'running'])
const ENDED_BADLY = new Set(['failed', 'lost', 'error', 'expired', 'cancelled', 'canceled', 'missed'])

export type CellState = 'replied' | 'hidden' | 'waiting' | 'no-reply' | 'failed'

export function cellState(c: HuddleCell): CellState {
  if (c.content_hidden) return 'hidden'
  if (c.block) return 'replied'
  if (PENDING.has(c.status)) return 'waiting'
  if (ENDED_BADLY.has(c.status)) return 'failed'
  return 'no-reply'
}

// ── co-sign arcs ─────────────────────────────────────────────────────────────

export type Answer = 'co-sign' | 'amend' | 'decline' | 'pending'

export function normAnswer(raw: unknown): Answer {
  const s = String(raw ?? '').toLowerCase().replace(/[^a-z]/g, '')
  if (s.startsWith('cosign')) return 'co-sign'
  if (s.startsWith('amend')) return 'amend'
  if (s.startsWith('decline')) return 'decline'
  return 'pending'
}

const norm = (s: unknown) => String(s ?? '').toLowerCase().replace(/\W+/g, ' ').trim()

export type Arc = {
  key: string
  title: string
  lead: string
  partner: string
  state: Answer
  note: string
  /** `data-anchor` ids: a cell is `<member>-<round>`, a column head `head-<member>`. */
  from: string
  to: string
}

type Proposal = { title?: unknown; lead?: unknown; with?: unknown }
type AnswerRow = { title?: unknown; lead?: unknown; answer?: unknown; note?: unknown }

function list<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/**
 * One arc per (joint proposal, partner): from the partner's round-3 reply to the
 * round-2 cell where the proposal was made, coloured by the partner's answer.
 * A partner who has not answered yet gets a dashed `pending` arc from its column
 * head; an answer to a proposal we cannot see (its round-2 cell hidden or not
 * replied) still draws, to the lead's column head.
 */
export function arcsFor(h: Pick<Huddle, 'cells'>): Arc[] {
  const arcs = new Map<string, Arc>()
  const id = (partner: string, lead: string, title: string) => `${partner}|${lead}|${norm(title)}`

  for (const c of h.cells) {
    if (c.round !== 2 || !c.block) continue
    for (const p of list<Proposal>((c.block as Block).proposals)) {
      const lead = String(p.lead || c.member)
      const title = String(p.title ?? '')
      for (const partner of list<string>(p.with).map(String)) {
        if (!partner || partner === lead) continue
        const key = id(partner, lead, title)
        arcs.set(key, {
          key, title, lead, partner, state: 'pending', note: '',
          from: h.cells.some((x) => x.member === partner && x.round === 3) ? `${partner}-3` : `head-${partner}`,
          to: `${c.member}-2`,
        })
      }
    }
  }
  for (const c of h.cells) {
    if (c.round !== 3 || !c.block) continue
    for (const a of list<AnswerRow>((c.block as Block).answers)) {
      const lead = String(a.lead ?? '')
      const title = String(a.title ?? '')
      let key = id(c.member, lead, title)
      // A partner may omit or misspell `lead`; fall back to a title-only match.
      if (!arcs.has(key)) {
        const byTitle = [...arcs.values()].find((x) => x.partner === c.member && norm(x.title) === norm(title))
        if (byTitle) key = byTitle.key
      }
      const prev = arcs.get(key)
      const to = prev?.to ?? (lead && h.cells.some((x) => x.member === lead && x.round === 2) ? `${lead}-2` : `head-${lead || c.member}`)
      arcs.set(key, {
        key, title: prev?.title ?? title, lead: prev?.lead ?? lead, partner: c.member,
        state: normAnswer(a.answer), note: String(a.note ?? ''), from: `${c.member}-3`, to,
      })
    }
  }
  return [...arcs.values()].filter((a) => a.from !== a.to)
}

/** `data-anchor` ids for the finer arc ends: the proposal card, the answer row. */
export const anchorKey = {
  proposal: (lead: string, title: string) => `prop|${lead}|${title.toLowerCase().trim()}`,
  answer: (member: string, title: string) => `ans|${member}|${title.toLowerCase().trim()}`,
}

// ── the leader's critique ────────────────────────────────────────────────────

/**
 * The leader's questions, as quoted into a round-2/3 prompt by the `work`
 * templates ("<leader>'s questions for you:" / "… on your own proposals …:").
 * Returns '' when there is none, or it is the template's "none".
 */
export function critiqueFrom(prompt: string, leader: string): string {
  if (!prompt || !leader) return ''
  const esc = leader.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = new RegExp(`^${esc}'s questions[^\\n]*:\\s*\\n([\\s\\S]*?)(?:\\n\\s*\\n|$)`, 'mi').exec(prompt)
  const text = (m?.[1] ?? '').trim()
  return /^(none\.?|n\/a|-)$/i.test(text) ? '' : text
}

// ── member colour ────────────────────────────────────────────────────────────

/** Semantic tokens only, so both themes hold. Assigned by column order, so a
 * huddle's members never share a colour until there are more than six. */
const HUES = ['--primary', '--info', '--special', '--success', '--warning', '--chart-2']

export function memberHue(index: number): string {
  return `var(${HUES[index % HUES.length]})`
}

export function initial(slug: string): string {
  return (slug.trim()[0] ?? '?').toUpperCase()
}

/** "42m" / "1h 05m" until `iso`; '' once passed. `now` injected for tests. */
export function countdown(iso: string | null | undefined, now: Date): string {
  if (!iso) return ''
  const ms = new Date(iso).getTime() - now.getTime()
  if (!(ms > 0)) return ''
  const mins = Math.ceil(ms / 60000)
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`
}
