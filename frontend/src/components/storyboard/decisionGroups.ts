import type { Decision, Storyboard } from '@/api/storyboards'

/**
 * The set's decision log — ace-web's pattern (spec B). Asks and assumptions
 * are the same row in different states, so ONE row component renders them all;
 * the grouping puts what the reader must do first, and each row appears once.
 */
export interface DecisionGroup {
  title: string
  open: boolean
  rows: Decision[]
  /** Set on "What the agent assumed" only: its rows again, by what they are about. */
  byScope?: { scope: string; label: string; rows: Decision[] }[]
}

/** Narrative slug → the title the board shows for it. */
export function narrativeTitles(board: Pick<Storyboard, 'acts'>): Map<string, string> {
  const titles = new Map<string, string>()
  for (const act of board.acts ?? []) {
    for (const e of act.entries ?? []) {
      if (e.title && !titles.has(e.narrative_slug)) titles.set(e.narrative_slug, e.title)
    }
  }
  return titles
}

/** What a row is about, in words: never the slug (no ids in rendered copy). */
export function scopeLabel(scope: string, titles: Map<string, string>): string {
  if (!scope) return 'Whole set'
  return titles.get(scope) || 'One demo'
}

/** Rows split by scope: the whole set first, then each narrative in order of first appearance. */
export function groupByScope(rows: Decision[], titles: Map<string, string>): { scope: string; label: string; rows: Decision[] }[] {
  const byScope = new Map<string, Decision[]>()
  for (const d of [...rows.filter((r) => !r.scope), ...rows.filter((r) => r.scope)]) {
    const list = byScope.get(d.scope) ?? []
    list.push(d)
    byScope.set(d.scope, list)
  }
  return [...byScope].map(([scope, rs]) => ({ scope, label: scopeLabel(scope, titles), rows: rs }))
}

export function groupDecisions(rows: Decision[], titles: Map<string, string> = new Map()): DecisionGroup[] {
  const live = rows.filter((d) => !d.superseded_by)
  const groups: DecisionGroup[] = [
    { title: 'Answer before sharing', open: true,
      rows: live.filter((d) => d.status !== 'deferred' && d.review_ask === 'required-before' && d.needed_by === 'external-share') },
    { title: 'Please confirm', open: true,
      rows: live.filter((d) => d.status !== 'deferred' && d.review_ask !== '' &&
        !(d.review_ask === 'required-before' && d.needed_by === 'external-share')) },
    { title: 'What the agent assumed', open: false,
      rows: live.filter((d) => d.status !== 'deferred' && !d.review_ask) },
    { title: 'Not needed now', open: false, rows: live.filter((d) => d.status === 'deferred') },
    { title: 'Replaced', open: false, rows: rows.filter((d) => d.superseded_by) },
  ]
  const assumed: DecisionGroup = groups[2]
  assumed.byScope = groupByScope(assumed.rows, titles)
  return groups.filter((g) => g.rows.length > 0)
}
