import type { Note } from '@/api/storyboards'

/**
 * What came back, shown to the people who sent the link and to nobody else.
 *
 * Rendered IN PLACE — under the act or the demo the note was left on — because
 * that is the only reading of it that carries meaning. A flat list of forty
 * comments detached from what they are about is the thing this whole surface
 * exists to avoid.
 */

function when(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function NotesReturned({ notes, label }: { notes: Note[]; label?: string }) {
  if (notes.length === 0) return null
  return (
    <div className="mt-3 flex flex-col gap-2 rounded-lg border border-info/25 bg-info/[0.06] p-3">
      <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-info">
        {label ?? (notes.length === 1 ? '1 note back' : `${notes.length} notes back`)}
      </span>
      {notes.map((n) => (
        <article key={n.id} className="flex flex-col gap-1 border-t border-info/15 pt-2 first:border-t-0 first:pt-0">
          <div className="flex flex-wrap items-baseline gap-2 text-[11px] text-muted-foreground">
            <span className="font-medium text-foreground-secondary">
              {n.author_name || 'Anonymous'}
            </span>
            {n.kind === 'suggestion' && (
              <span className="rounded-full border border-special/30 bg-special/10 px-1.5 py-px text-[10px] text-special">
                Narrative edit
              </span>
            )}
            {n.target_version != null && <span className="tabular-nums">v{n.target_version}</span>}
            {n.channel !== 'web' && <span>via {n.channel}</span>}
            <span>{when(n.created_at)}</span>
            {n.state !== 'open' && <span className="italic">{n.state}</span>}
          </div>
          <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">
            {n.body || n.suggested_text}
          </p>
        </article>
      ))}
    </div>
  )
}

/** Group notes by where they belong on the board. */
export function groupNotes(notes: Note[]) {
  const byAnchor = new Map<string, Note[]>()
  const byNarrative = new Map<string, Note[]>()
  const loose: Note[] = []

  for (const n of notes) {
    if (n.target_kind === 'narrative') {
      const list = byNarrative.get(n.target_ref) ?? []
      list.push(n)
      byNarrative.set(n.target_ref, list)
    } else if (n.anchor_id) {
      const list = byAnchor.get(n.anchor_id) ?? []
      list.push(n)
      byAnchor.set(n.anchor_id, list)
    } else {
      // Board-level notes, and act notes left before acts carried an anchor.
      loose.push(n)
    }
  }
  return { byAnchor, byNarrative, loose }
}
