/**
 * The pure half of the huddle pages: round names, who-answered-what for the
 * co-sign arcs, the leader's critique pulled out of a round prompt, and a stable
 * colour per member. No React here, so all of it is unit-testable.
 *
 * A huddle is DERIVED server-side (apps/huddles): the API hands back one cell per
 * (member, round) with the member's parsed reply block. Nothing below invents
 * state — an arc is drawn only from what blocks actually say.
 */
import type { Huddle, HuddleCell, HuddleOutput } from '@/api/huddles'
import type { AgentThread } from '@/api/threads'
import { holdSentence, sizePlain, stepAsk, stepName, taskStatusPlain } from './plainWords'

export type Block = Record<string, unknown>

/** The engine's rounds per huddle type (its own names stay in the engine; the
 * page shows them as plain steps — see plainWords). */
const ROUND_NAMES: Record<string, Record<number, string>> = {
  work: { 1: 'report', 2: 'roundtable', 3: 'co-sign', 4: 'resolve' },
}

/** The rounds a type always runs, shown before they start. Any round past these
 * (work's round 4 "resolve" only runs when a partner answered `amend`) is shown
 * once it is dispatched or has a cell. */
const ALWAYS_ROUNDS: Record<string, number> = { work: 3 }

/** The round's name as the page shows it: a plain step name. */
export function roundName(type: string, round: number): string {
  return stepName(type, round)
}

/** Rounds the grid shows: every type round up to the furthest one dispatched,
 * so a work huddle in round 1 still shows what is coming. */
export function roundsToShow(h: Pick<Huddle, 'type' | 'rounds_dispatched' | 'cells'>): number[] {
  const always = ALWAYS_ROUNDS[h.type] ?? Math.max(0, ...Object.keys(ROUND_NAMES[h.type] ?? {}).map(Number))
  const seen = h.cells.map((c) => c.round)
  const top = Math.max(h.rounds_dispatched, always, ...seen, 1)
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

/** An arc's state: the partner's answer, or — for an `amend` the lead resolved
 * in round 4 — whether the lead accepted the amend (now a co-sign) or rejected
 * it (the proposal stays held). */
export type ArcState = Answer | 'amend-accepted' | 'amend-rejected'

export type Resolution = 'accept' | 'reject'

export function normResolution(raw: unknown): Resolution | null {
  const s = String(raw ?? '').toLowerCase().replace(/[^a-z]/g, '')
  if (s.startsWith('accept')) return 'accept'
  if (s.startsWith('reject')) return 'reject'
  return null
}

export type Arc = {
  key: string
  title: string
  lead: string
  partner: string
  state: ArcState
  note: string
  /** `data-anchor` ids: a cell is `<member>-<round>`, a column head `head-<member>`. */
  from: string
  to: string
  /** The agreement thread where the lead and this partner settled the changes. */
  thread?: string
}

/** What a huddle may carry beside its cells: the agent threads hanging off it. */
type WithThreads = { id?: string; threads?: AgentThread[] }

/** The agreement threads of a huddle: one per "in, with changes" answer, where
 * the idea's lead (`parent.lead`, the author) and the teammate who asked for the
 * changes (the asker) settle them directly instead of through the leader. */
export function agreementThreads(h: WithThreads): AgentThread[] {
  return (h.threads ?? []).filter((t) => {
    const parent = (t.parent ?? {}) as Record<string, unknown>
    return t.kind === 'agreement' && (!h.id || !parent.huddle || String(parent.huddle) === h.id)
  })
}

/** {lead, asker, title} of an agreement thread: the asker is the participant whose
 * role says so, else the one who is not the lead. */
export function agreementParties(t: AgentThread): { lead: string; asker: string; title: string } {
  const parent = (t.parent ?? {}) as Record<string, unknown>
  const ps = (Array.isArray(t.participants) ? t.participants : []) as { agent?: unknown; role?: unknown }[]
  const lead = String(parent.lead ?? ps.find((p) => String(p.role) === 'author')?.agent ?? '')
  const asker = String(ps.find((p) => String(p.role) === 'asker')?.agent ?? ps.find((p) => String(p.agent) !== lead)?.agent ?? '')
  return { lead, asker, title: String(parent.title ?? '') }
}

type Proposal = { title?: unknown; lead?: unknown; with?: unknown }
type AnswerRow = { title?: unknown; lead?: unknown; answer?: unknown; note?: unknown }
type ResolutionRow = { title?: unknown; lead?: unknown; resolution?: unknown; note?: unknown }

function list<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/**
 * One arc per (joint proposal, partner): from the partner's round-3 reply to the
 * round-2 cell where the proposal was made, coloured by the partner's answer.
 * A partner who has not answered yet gets a dashed `pending` arc from its column
 * head; an answer to a proposal we cannot see (its round-2 cell hidden or not
 * replied) still draws, to the lead's column head. An `amend` the lead then
 * resolved in round 4 becomes `amend-accepted` / `amend-rejected`.
 */
export function arcsFor(h: Pick<Huddle, 'cells'> & WithThreads): Arc[] {
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
  for (const c of h.cells) {
    if (c.round !== 4 || !c.block) continue
    for (const r of list<ResolutionRow>((c.block as Block).resolutions)) {
      const verdict = normResolution(r.resolution)
      if (!verdict) continue
      // Only the proposal's own lead can resolve an amend to it.
      const lead = String(r.lead || c.member)
      if (lead !== c.member) continue
      for (const a of arcs.values()) {
        if (a.lead === lead && a.state === 'amend' && norm(a.title) === norm(r.title)) {
          a.state = verdict === 'accept' ? 'amend-accepted' : 'amend-rejected'
        }
      }
    }
  }
  // An agreement thread settles an amend directly between the lead and the
  // partner: once it closes, agreed → the changes are agreed (a co-sign), and
  // anything else (not agreed, out of messages, out of time) → not agreed.
  for (const t of agreementThreads(h)) {
    const { lead, asker, title } = agreementParties(t)
    for (const a of arcs.values()) {
      if (a.lead !== lead || a.partner !== asker || norm(a.title) !== norm(title)) continue
      if (!['amend', 'amend-accepted', 'amend-rejected'].includes(a.state)) continue
      a.thread = t.id
      if (t.status !== 'open') {
        const agreed = t.status === 'settled' && String((t.outcome as Record<string, unknown>)?.result ?? '') === 'agreed'
        a.state = agreed ? 'amend-accepted' : 'amend-rejected'
      }
    }
  }
  return [...arcs.values()].filter((a) => a.from !== a.to)
}

/** `data-anchor` ids for the finer arc ends: the proposal card, the answer row. */
export const anchorKey = {
  proposal: (lead: string, title: string) => `prop|${lead}|${title.toLowerCase().trim()}`,
  answer: (member: string, title: string) => `ans|${member}|${title.toLowerCase().trim()}`,
}

// ── the outcome: what each proposal became ──────────────────────────────────

/** A proposal as made in round 2, flattened for the outcome summary. */
export type ProposalInfo = {
  key: string
  title: string
  lead: string
  /** Who must co-sign — `with` minus the lead. Empty for a solo proposal. */
  partners: string[]
  /** The member whose round-2 reply carried it (may differ from the lead). */
  proposedBy: string
  priority: string
  project: string
  effort: string
  confidence: number | null
  why: string
}

/**
 * - `filed`  — on the board (it has tasks), or agreed and waiting to be filed;
 * - `held`   — the huddle is over and it did not get through;
 * - `open`   — the huddle is still running and it is not settled yet.
 */
export type Verdict = 'filed' | 'held' | 'open'

/** Why a proposal is held (or still open), and the partners it is about. */
export type Hold =
  | { kind: 'declined'; who: string[] }
  | { kind: 'amend-rejected'; who: string[] }
  | { kind: 'amend'; who: string[] }
  | { kind: 'pending'; who: string[] }
  /** Every partner co-signed but the leader's filing gates held it. */
  | { kind: 'gate'; who: string[] }

export type ProposalOutcome = ProposalInfo & {
  verdict: Verdict
  hold: Hold | null
  /** Each partner's answer arc (the same arcs the conversation draws). */
  arcs: Arc[]
  /** The board tasks it produced: the lead's first, then one per partner. */
  tasks: HuddleOutput[]
}

export type Outcome = {
  filed: ProposalOutcome[]
  held: ProposalOutcome[]
  open: ProposalOutcome[]
  /** Board tasks pointing at this huddle that match no proposal we can see. */
  unmatched: HuddleOutput[]
}

/** "Pre-flight … — eva's part (lead ace)": the per-partner task title the
 * huddle engine files for a joint proposal (canopy `huddle_cli`). */
const PART_TITLE = /^(.*?)\s+[—–-]+\s+(\S+?)['’]s part \(lead ([^)]+)\)\s*$/

/** Which proposal a board task belongs to. The API carries no structured link
 * (a task only knows the huddle page it came from), so this matches by title:
 * the lead's task IS the proposal title, each partner's is "<title> — <m>'s part
 * (lead <lead>)". */
export function taskProposalKey(o: Pick<HuddleOutput, 'title' | 'agent'>, proposals: ProposalInfo[]): string | null {
  const m = PART_TITLE.exec(o.title)
  if (m) {
    const [, base, , lead] = m
    const hit = proposals.find((p) => norm(p.title) === norm(base) && p.lead === lead.trim())
      ?? proposals.find((p) => norm(p.title) === norm(base))
    return hit?.key ?? null
  }
  const same = proposals.filter((p) => norm(p.title) === norm(o.title))
  return (same.find((p) => p.lead === o.agent) ?? same[0])?.key ?? null
}

type ProposalRaw = Proposal & {
  priority?: unknown; project?: unknown; effort?: unknown; confidence?: unknown; why?: unknown
}

/** Every round-2 proposal, once each (lead + title), in column then card order. */
export function proposalsOf(h: Pick<Huddle, 'cells' | 'members'>): ProposalInfo[] {
  const cols = columns(h)
  const cells = h.cells
    .filter((c) => c.round === 2 && c.block)
    .sort((a, b) => cols.indexOf(a.member) - cols.indexOf(b.member))
  const out = new Map<string, ProposalInfo>()
  for (const c of cells) {
    for (const p of list<ProposalRaw>((c.block as Block).proposals)) {
      const lead = String(p.lead || c.member)
      const title = String(p.title ?? '').trim()
      if (!title) continue
      const key = `${lead}|${norm(title)}`
      if (out.has(key)) continue
      const project = p.project && typeof p.project === 'object' ? (p.project as { name?: unknown }).name : p.project
      out.set(key, {
        key, title, lead, proposedBy: c.member,
        partners: [...new Set(list<string>(p.with).map(String))].filter((m) => m && m !== lead),
        priority: String(p.priority ?? ''),
        project: String(project ?? ''),
        effort: String(p.effort ?? ''),
        confidence: typeof p.confidence === 'number' ? p.confidence : null,
        why: String(p.why ?? ''),
      })
    }
  }
  return [...out.values()]
}

/** The partners' answers decide it, read off the same arcs the grid draws — so a
 * round-4 accepted amend counts as a co-sign and a rejected one holds (canopy's
 * `work_gates` order: a decline, then a rejected amend, then an open amend, then
 * a missing answer). */
function holdFrom(arcs: Arc[]): Hold | null {
  const who = (...s: ArcState[]) => arcs.filter((a) => s.includes(a.state)).map((a) => a.partner)
  if (who('decline').length) return { kind: 'declined', who: who('decline') }
  if (who('amend-rejected').length) return { kind: 'amend-rejected', who: who('amend-rejected') }
  if (who('amend').length) return { kind: 'amend', who: who('amend') }
  if (who('pending').length) return { kind: 'pending', who: who('pending') }
  return null
}

/** What the huddle produced, proposal by proposal: the filed ones with their
 * board tasks grouped beneath, the held ones with why. A proposal with tasks is
 * filed whatever its arcs say — the board is the record of what was filed. */
export function outcomeOf(h: Pick<Huddle, 'cells' | 'members' | 'outputs' | 'finished'> & WithThreads): Outcome {
  const proposals = proposalsOf(h)
  const arcs = arcsFor(h)
  const tasks = new Map<string, HuddleOutput[]>()
  const unmatched: HuddleOutput[] = []
  for (const o of h.outputs) {
    const k = taskProposalKey(o, proposals)
    if (k) tasks.set(k, [...(tasks.get(k) ?? []), o])
    else unmatched.push(o)
  }
  const out: Outcome = { filed: [], held: [], open: [], unmatched }
  for (const p of proposals) {
    const mine = arcs.filter((a) => a.lead === p.lead && norm(a.title) === norm(p.title) && p.partners.includes(a.partner))
    // A partner with no arc at all (its answer never parsed) is still owed one.
    for (const m of p.partners) {
      if (!mine.some((a) => a.partner === m)) {
        mine.push({ key: `${m}|${p.key}`, title: p.title, lead: p.lead, partner: m, state: 'pending', note: '', from: `head-${m}`, to: `${p.proposedBy}-2` })
      }
    }
    const ts = (tasks.get(p.key) ?? []).sort((a, b) => Number(b.agent === p.lead) - Number(a.agent === p.lead))
    const hold = holdFrom(mine)
    let verdict: Verdict
    let why: Hold | null = hold
    if (ts.length) {
      verdict = 'filed'
      why = null
    } else if (!h.finished) {
      verdict = hold ? 'open' : 'filed'
    } else {
      verdict = 'held'
      why = hold ?? { kind: 'gate', who: [] }
    }
    const row: ProposalOutcome = { ...p, verdict, hold: why, arcs: mine, tasks: ts }
    out[verdict].push(row)
  }
  return out
}

/** Plain words for the proposal's shorthand: "small job · 60% sure it's worth it". */
export function sizeWords(effort: string, confidence: number | null): string {
  return sizePlain(effort, confidence)
}

/** A board task's status, as the person reading the huddle has to act on it.
 * `suggested` is the board's inbox: a task waiting for its owner to Accept or
 * Decline (TasksBoard). In progress, `assigned` is who the next step waits on. */
export function taskStatusWords(o: Pick<HuddleOutput, 'status' | 'assigned'>): string {
  return taskStatusPlain(o.status)
}

/** Why a proposal is parked (or not settled yet), and what would clear it. */
export function holdWords(p: Pick<ProposalOutcome, 'hold' | 'lead' | 'verdict'> & { proposedBy?: string }, leader: string): { why: string; clear: string } {
  return holdSentence(p.hold, { lead: p.lead, proposedBy: p.proposedBy, leader, open: p.verdict === 'open' })
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

// ── the leader's side of the conversation ───────────────────────────────────

/** What the leader asks of everyone in a round, in a few plain words. */
export function roundAsk(type: string, round: number): string {
  return stepAsk(type, round)
}

/** One question the leader put to a member. `about` names the proposal it is
 * about (round 3's critiques), or '' for a question to the member at large. */
export type LeaderAsk = { about: string; text: string }

const NONE = /^(none\.?|n\/a|-)$/i
const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/

/**
 * The questions the leader sent one member, read out of that member's round
 * prompt — the critique sections the `work` engine renders:
 *  - round 2: bullets under "<leader>'s questions for you:";
 *  - round 3: a "Critique: …" paragraph after each "### <title> (lead x)" joint
 *    proposal, and bullets under "<leader>'s questions on your own proposals …:".
 * Defensive: a header with prose instead of bullets is one question; "none" is
 * none; anything it cannot read returns [] (the card then offers the prompt).
 */
export function leaderAsks(prompt: string, leader: string): LeaderAsk[] {
  if (!prompt || !leader) return []
  const esc = leader.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const header = new RegExp(`^${esc}['’]s questions\\b.*:\\s*$`, 'i')
  const lines = prompt.split('\n')
  const out: LeaderAsk[] = []
  let title = ''
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim()
    const h3 = /^###\s+(.+?)(?:\s+\(lead [^)]*\))?\s*$/.exec(line)
    if (h3) {
      title = h3[1].trim()
      continue
    }
    const crit = /^Critique:\s*(.*)$/i.exec(line)
    if (crit) {
      const para = [crit[1]]
      while (i + 1 < lines.length && lines[i + 1].trim()) para.push(lines[++i].trim())
      const text = para.join(' ').trim()
      if (text && !NONE.test(text)) out.push({ about: title, text })
      continue
    }
    if (!header.test(line)) continue
    const about = /own proposals/i.test(line) ? 'your own proposals' : ''
    const items: string[] = []
    let j = i + 1
    while (j < lines.length && !lines[j].trim()) j++
    for (; j < lines.length; j++) {
      const l = lines[j]
      if (!l.trim()) {
        // A blank line ends the section unless another bullet follows it.
        let k = j + 1
        while (k < lines.length && !lines[k].trim()) k++
        if (k < lines.length && BULLET.test(lines[k]) && items.length) { j = k - 1; continue }
        break
      }
      if (BULLET.test(l)) items.push(l.replace(BULLET, '').trim())
      else if (items.length) items[items.length - 1] += ' ' + l.trim()
      else items.push(l.trim())
    }
    i = j
    for (const t of items) if (t && !NONE.test(t)) out.push({ about, text: t })
  }
  return out
}

// ── compact card summaries ──────────────────────────────────────────────────

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const str = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'string' ? v : String(v))

/** Round 1: "5 worked on · 4 priorities · 3 needs", plus the top priority. */
export function reportSummary(b: Block): { stats: string; top: string } {
  const n = (k: string) => list<unknown>(b[k]).length
  const parts = [
    n('worked_on') ? `${n('worked_on')} worked on` : '',
    n('priorities') ? plural(n('priorities'), 'priority', 'priorities') : '',
    n('needs') ? plural(n('needs'), 'need') : '',
  ].filter(Boolean)
  const top = list<unknown>(b.priorities)[0]
  return { stats: parts.join(' · '), top: top === undefined ? '' : typeof top === 'string' ? top : JSON.stringify(top) }
}

export type ProposalLine = { title: string; lead: string; partners: string[]; anchor: string }

/** Round 2: one line per proposal (title, partners), anchored for the arcs. */
export function proposalLines(b: Block, member: string): ProposalLine[] {
  return list<Proposal>(b.proposals).map((p) => {
    const lead = str(p.lead) || member
    const title = str(p.title)
    return {
      title, lead,
      partners: [...new Set(list<unknown>(p.with).map(str))].filter((m) => m && m !== lead),
      anchor: anchorKey.proposal(lead, title),
    }
  })
}

export type AnswerLine = { title: string; lead: string; answer: Answer; anchor: string }

/** Round 3: one row per answer, anchored for the arcs. */
export function answerLines(b: Block, member: string): AnswerLine[] {
  return list<AnswerRow>(b.answers).map((a) => ({
    title: str(a.title), lead: str(a.lead), answer: normAnswer(a.answer), anchor: anchorKey.answer(member, str(a.title)),
  }))
}

/** Round 4: accept / reject + title. */
export function resolutionLines(b: Block): { title: string; verdict: Resolution | null }[] {
  return list<ResolutionRow>(b.resolutions).map((r) => ({ title: str(r.title), verdict: normResolution(r.resolution) }))
}

/** "answered 3 of ada's questions" — round 2's `critique_answers`. */
export function critiqueAnswered(b: Block, leader: string): string {
  const n = list<unknown>(b.critique_answers).length
  return n ? `answered ${n} of ${leader.charAt(0).toUpperCase() + leader.slice(1)}'s question${n === 1 ? '' : 's'}` : ''
}

// ── idea colour ──────────────────────────────────────────────────────────────

/** One colour and letter per idea, so a reader can follow an idea down the
 * Story from step 2 to its answers to what became of it. Mid-lightness hues
 * that read on both themes, distinct from the answer colours. */
const IDEA_HUES = [
  'oklch(0.62 0.16 262)', 'oklch(0.66 0.15 195)', 'oklch(0.62 0.18 320)',
  'oklch(0.68 0.14 75)', 'oklch(0.6 0.15 25)', 'oklch(0.64 0.12 150)',
]

export function ideaHue(index: number): string {
  return IDEA_HUES[index % IDEA_HUES.length]
}

export function ideaLetter(index: number): string {
  return String.fromCharCode(65 + (index % 26))
}
