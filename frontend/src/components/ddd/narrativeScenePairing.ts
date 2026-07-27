import type { DddNarration } from '../../api/ddd'

/**
 * A before/after pairing of one scene across two narrative versions.
 *
 * `before` is the prior version's narration text (null when the scene is new);
 * `after` is the proposed version's text (null when the scene was removed).
 */
export interface NarrativeScenePair {
  id: string | null
  title: string | null
  before: string | null
  after: string | null
  status: 'unchanged' | 'changed' | 'added' | 'removed'
}

function keyOf(n: DddNarration, i: number): string {
  // Scenes reused across versions keep a stable `id` — match on it so a diff is
  // robust to reordering. Id-less scenes fall back to positional pairing.
  const id = n.id != null ? String(n.id).trim() : ''
  return id || `#${i}`
}

function textOf(n: DddNarration): string {
  return typeof n.text === 'string' ? n.text : ''
}

function norm(s: string): string {
  return s.replace(/\s+/g, ' ').trim()
}


/** How many scene ids appear on both sides. 0 means the ids are incomparable. */
function sharedIdCount(before: DddNarration[], after: DddNarration[]): number {
  const ids = (list: DddNarration[]) =>
    new Set(list.map((n) => (n.id != null ? String(n.id).trim() : '')).filter(Boolean))
  const a = ids(before)
  if (a.size === 0) return 0
  let shared = 0
  ids(after).forEach((id) => {
    if (a.has(id)) shared += 1
  })
  return shared
}

/** Index-for-index pairing, for histories whose ids cannot be compared. */
function pairByPosition(before: DddNarration[], after: DddNarration[]): NarrativeScenePair[] {
  const pairs: NarrativeScenePair[] = []
  const len = Math.max(before.length, after.length)
  for (let i = 0; i < len; i += 1) {
    const b = before[i]
    const a = after[i]
    if (a && b) {
      const beforeText = textOf(b)
      const afterText = textOf(a)
      pairs.push({
        id: a.id != null ? String(a.id) : null,
        title: a.title ?? b.title ?? null,
        before: beforeText,
        after: afterText,
        status: norm(beforeText) === norm(afterText) ? 'unchanged' : 'changed',
      })
    } else if (a) {
      pairs.push({ id: a.id != null ? String(a.id) : null, title: a.title ?? null, before: null, after: textOf(a), status: 'added' })
    } else if (b) {
      pairs.push({ id: b.id != null ? String(b.id) : null, title: b.title ?? null, before: textOf(b), after: null, status: 'removed' })
    }
  }
  return pairs
}

/**
 * Pair the scenes of two narrative versions for a plain-language before/after.
 *
 * Output order follows the `after` (proposed) version — the story the reviewer is
 * being asked to approve — with any removed scenes appended at the end. Scenes are
 * matched by `id` where present (reorder-safe), else by position.
 */
export function pairNarrationScenes(
  before: DddNarration[],
  after: DddNarration[],
): NarrativeScenePair[] {
  // Legacy histories pair POSITIONALLY, because their ids were derived from
  // scene titles — so a version where the author reworded every title shares no
  // id with its predecessor, and matching on id would report every scene as
  // added+removed. That is precisely the case a domain expert cares about: on
  // `verified-monitoring` v16→v17, zero of six ids overlapped and the diff
  // would have shown six "New" scenes and no before/after at all.
  //
  // Zero overlap is the signal — with the stable ids authored since L0, a real
  // rewrite still shares most ids, so this never fires on new work. Requiring
  // BOTH sides non-empty keeps a genuine "all scenes replaced" edit honest.
  if (before.length > 0 && after.length > 0 && sharedIdCount(before, after) === 0) {
    return pairByPosition(before, after)
  }

  const beforeByKey = new Map<string, DddNarration>()
  before.forEach((n, i) => {
    const k = keyOf(n, i)
    if (!beforeByKey.has(k)) beforeByKey.set(k, n)
  })

  const consumed = new Set<string>()
  const pairs: NarrativeScenePair[] = []

  after.forEach((n, i) => {
    const k = keyOf(n, i)
    const b = beforeByKey.get(k)
    if (b && !consumed.has(k)) {
      consumed.add(k)
      const beforeText = textOf(b)
      const afterText = textOf(n)
      pairs.push({
        id: n.id != null ? String(n.id) : null,
        title: n.title ?? b.title ?? null,
        before: beforeText,
        after: afterText,
        status: norm(beforeText) === norm(afterText) ? 'unchanged' : 'changed',
      })
    } else {
      pairs.push({
        id: n.id != null ? String(n.id) : null,
        title: n.title ?? null,
        before: null,
        after: textOf(n),
        status: 'added',
      })
    }
  })

  before.forEach((n, i) => {
    const k = keyOf(n, i)
    if (!consumed.has(k)) {
      pairs.push({
        id: n.id != null ? String(n.id) : null,
        title: n.title ?? null,
        before: textOf(n),
        after: null,
        status: 'removed',
      })
    }
  })

  return pairs
}

/** True when any scene differs between the two versions. */
export function hasNarrativeChanges(pairs: NarrativeScenePair[]): boolean {
  return pairs.some((p) => p.status !== 'unchanged')
}
