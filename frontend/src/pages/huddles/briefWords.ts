/**
 * The principal's priorities brief, and the fields a brief-era huddle adds to a
 * reply, as plain words (pure — unit-tested in briefWords.test.ts).
 *
 * Since 2026-10-09 a huddle runs on a BRIEF: 3–5 numbered priorities with hard
 * dates (canopy `huddle plan --priorities-file`; the anchor carries it, the API
 * hands it back as `priorities_brief`). Members then report `levers` (where they
 * can move a numbered priority) instead of guessing the priorities, and an idea
 * names its priority by NUMBER, says what it costs Jonathan (`cost_to_jonathan`)
 * and what would make it fail (`fails_if`) instead of a confidence %. Huddles
 * from before keep their old fields, and every helper here reads both.
 */
import { deJargon, trimPriority } from './plainWords'

export type BriefItem = { n: number; text: string; dates: string; source: string }

const ITEM = /^\s*#?(\d+)[.):]\s+(.+?)\s*$/

/** The brief's numbered lines: "2. IDM keynote — hard dates: 2026-10-15 — source: calendar". */
export function briefItems(brief: string | null | undefined): BriefItem[] {
  const out: BriefItem[] = []
  for (const line of String(brief ?? '').split('\n')) {
    const m = ITEM.exec(line)
    if (!m || out.some((i) => i.n === Number(m[1]))) continue
    const [text, ...rest] = m[2].split(/\s+[—–]\s+/)
    const field = (name: string) => rest.find((r) => r.toLowerCase().startsWith(`${name}:`))?.slice(name.length + 1).trim() ?? ''
    out.push({ n: Number(m[1]), text: text.trim(), dates: field('hard dates'), source: field('source') })
  }
  return out
}

/** The brief's optional "Not now: …" line. */
export function briefNotNow(brief: string | null | undefined): string {
  const m = /^\s*not now:\s*(.+)$/im.exec(String(brief ?? ''))
  return m ? m[1].trim() : ''
}

/** A priority as a brief number — 2, "2", "#2", "2. IDM…" — or null for free text. */
export function priorityNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isInteger(v)) return v
  if (typeof v !== 'string') return null
  const m = /^\s*(?:priority\s*)?#?(\d+)\b/i.exec(v)
  return m ? Number(m[1]) : null
}

/** What an idea serves: "Serves priority 2" + the brief's words for it, or — an
 * older huddle's free-text priority — "For the priority" + that text. */
export function servesWords(priority: unknown, brief: string | null | undefined): { label: string; text: string } | null {
  const n = priorityNumber(priority)
  if (n !== null) {
    const item = briefItems(brief).find((i) => i.n === n)
    return { label: `Serves priority ${n}`, text: item ? trimPriority(item.text, 200) : '' }
  }
  const s = typeof priority === 'string' ? priority.trim() : ''
  return s ? { label: 'For the priority', text: trimPriority(s, 200) } : null
}

const COST_KIND: Record<string, string> = {
  yes: 'your yes', decision: 'a decision from you', time: 'some of your time',
}

/** "20-minute dry run Tuesday" — what an idea needs from Jonathan, or '' when
 * it needs nothing (kind `none`) or says nothing. */
export function costWords(cost: unknown): string {
  if (!cost || typeof cost !== 'object') return ''
  const c = cost as { kind?: unknown; detail?: unknown }
  const kind = String(c.kind ?? '').trim().toLowerCase()
  if (!kind || kind === 'none') return ''
  const detail = String(c.detail ?? '').trim()
  return deJargon(detail || COST_KIND[kind] || kind)
}

/** "Would fail if the demo env flakes on stage" — or '' with nothing said. */
export function failsWords(fails: unknown): string {
  const s = typeof fails === 'string' ? fails.trim().replace(/^(?:it\s+)?(?:would\s+)?fails?\s+if\s+/i, '').replace(/^if\s+/i, '') : ''
  return s ? `Would fail if ${deJargon(s.replace(/[.]+$/, ''))}` : ''
}

const LEVER_KIND: Record<string, (task: string) => string> = {
  new: () => 'new work',
  unblock: (t) => (t ? `unblocks ${t}` : 'unblocks something stuck'),
  existing: (t) => (t ? `already on the board as ${t}` : 'already on the board'),
}

/** One round-1 lever as a line: "Priority 2 (IDM keynote lands): stand up the
 * demo env — new work · checked". */
export function leverWords(lever: unknown, brief: string | null | undefined): string {
  if (!lever || typeof lever !== 'object') return deJargon(String(lever ?? ''))
  const l = lever as Record<string, unknown>
  const n = priorityNumber(l.priority)
  const item = n === null ? undefined : briefItems(brief).find((i) => i.n === n)
  const head = n === null ? '' : `Priority ${n}${item ? ` (${trimPriority(item.text, 60)})` : ''}: `
  const kind = LEVER_KIND[String(l.kind ?? '').toLowerCase()]?.(String(l.task ?? '').trim())
  const tail = [
    kind,
    String(l.blocked_by ?? '').trim() ? `blocked by ${String(l.blocked_by).trim()}` : '',
    l.verified === true ? 'checked' : l.verified === false ? 'not checked yet' : '',
  ].filter(Boolean).join(' · ')
  return deJargon(`${head}${String(l.move ?? '').trim()}${tail ? ` — ${tail}` : ''}`)
}

/** One round-1 need: a string as-is, or `{from, ask}` as "From Jonathan: …". */
export function needWords(need: unknown): string {
  if (!need || typeof need !== 'object') return deJargon(String(need ?? ''))
  const n = need as { from?: unknown; ask?: unknown }
  const from = String(n.from ?? '').trim()
  const who = from ? from.charAt(0).toUpperCase() + from.slice(1) : ''
  return deJargon(`${who ? `From ${who}: ` : ''}${String(n.ask ?? '').trim()}`)
}

/** A round-1 item as a line, whatever its key. */
export function reportItemWords(key: string, item: unknown, brief: string | null | undefined): string {
  if (key === 'levers') return leverWords(item, brief)
  if (key === 'needs') return needWords(item)
  return typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item ?? '')
}
