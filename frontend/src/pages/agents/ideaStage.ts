import type { IdeaOut } from '@/api/agents'

// An idea's stage, in a contributor's words (hal T69): a task's status read as
// proposed / started / shipped. The server does the mapping; this only names it.

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
