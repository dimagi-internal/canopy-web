import type { IdeaOut } from '@/api/agents'

// An idea's stage (hal T82): proposed until a task works on it, then started;
// shipped and not-pursued are a person's call. The server decides; this names it.

export type Stage = IdeaOut['stage']

export const STAGE_LABEL: Record<Stage, string> = {
  proposed: 'Proposed',
  started: 'Started',
  shipped: 'Shipped',
  declined: 'Not pursued',
}

/** A short date, for "said on". */
export function saidOn(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** "1 shipped · 2 started · 1 proposed" — the same three words everywhere. */
export function tally(c: { shipped: number; started: number; proposed: number }): string {
  return `${c.shipped} shipped · ${c.started} started · ${c.proposed} proposed`
}

/** A person whose page is not written (or not shown to this reader). */
export function unwritten(name: string): string {
  const first = name.split(' ')[0] || name
  return `${first} hasn't shared what they're after yet.`
}
