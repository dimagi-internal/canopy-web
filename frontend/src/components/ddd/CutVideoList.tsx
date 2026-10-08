import { withBase } from '@/lib/basePath'

/** One cut of a recorded narrative and the video pinned to it (canopy-web#1288).
 *  The narrative page and the review link both pass their own payload's cuts,
 *  which share this shape. */
export interface CutVideo {
  cut_id: string
  title: string
  /** Narration item (scene) ids the cut plays, in order. */
  scene_ids: string[]
  /** Null when the reader may not play it (a private video, a guest). */
  video_url: string | null
  video_viewer_url?: string | null
  duration_sec?: number | null
}

/** A scene's spoken words, keyed by its narration id. */
export interface CutScene {
  id: string
  title: string
  text: string
}

/**
 * Group scenes into the demo's cuts. A DDD scene title names its cut before an
 * em dash — "Cut 3 · Deliver — the proposed spot, before" — and consecutive
 * scenes sharing that prefix are one cut. A narrative whose titles carry no cut
 * prefix stays one group.
 */
export function groupScenesByCut<T extends { title: string }>(
  scenes: T[],
): Array<{ cut: string | null; scenes: T[] }> {
  const cutOf = (t: string) => {
    const i = (t ?? '').indexOf(' — ')
    return i > 0 ? t.slice(0, i).trim() : null
  }
  if (!scenes.some((s) => cutOf(s.title))) return [{ cut: null, scenes }]
  const groups: Array<{ cut: string | null; scenes: T[] }> = []
  for (const s of scenes) {
    const cut = cutOf(s.title)
    const last = groups[groups.length - 1]
    if (last && last.cut === cut) last.scenes.push(s)
    else groups.push({ cut, scenes: [s] })
  }
  return groups
}

type Row =
  | { kind: 'video'; pos: number; title: string; cut: CutVideo; words: CutScene[] }
  | { kind: 'missing'; pos: number; title: string; words: CutScene[] }

/** Every cut in narration order: the uploaded ones, plus any cut the narration
 *  names (by its scene-title prefix) that no upload covers yet — so a cut that
 *  was never rendered reads "not rendered yet" instead of silently missing. */
export function cutRows(cuts: CutVideo[], scenes: CutScene[]): Row[] {
  const pos = new Map(scenes.map((s, i) => [s.id, i]))
  const byId = new Map(scenes.map((s) => [s.id, s]))
  const covered = new Set(cuts.flatMap((c) => c.scene_ids))
  const end = scenes.length
  const rows: Row[] = cuts.map((cut) => {
    const places = cut.scene_ids.map((id) => pos.get(id)).filter((p): p is number => p !== undefined)
    return {
      kind: 'video',
      pos: places.length ? Math.min(...places) : end,
      title: cut.title,
      cut,
      words: cut.scene_ids.map((id) => byId.get(id)).filter((s): s is CutScene => s !== undefined),
    }
  })
  for (const group of groupScenesByCut(scenes)) {
    if (!group.cut || group.scenes.some((s) => covered.has(s.id))) continue
    rows.push({ kind: 'missing', pos: pos.get(group.scenes[0].id) ?? end, title: group.cut, words: group.scenes })
  }
  // Stable: two rows at the same position keep their upload order.
  return rows.map((r, i) => [r, i] as const).sort((a, b) => a[0].pos - b[0].pos || a[1] - b[1]).map(([r]) => r)
}

function fmtDuration(sec?: number | null): string | null {
  if (sec == null || sec <= 0) return null
  return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`
}

/**
 * A recorded narrative's cuts — "here are the short videos and what each says".
 * Each cut is its video beside the words it speaks. Shared by the narrative page
 * and the review link's Cuts tab so the two cannot show different things.
 */
export function CutVideoList({ cuts, scenes }: { cuts: CutVideo[]; scenes: CutScene[] }) {
  const rows = cutRows(cuts, scenes)
  return (
    <ol className="space-y-6">
      {rows.map((row, i) => {
        const duration = row.kind === 'video' ? fmtDuration(row.cut.duration_sec) : null
        const optional = /optional/i.test(row.title)
        return (
          <li key={row.kind === 'video' ? row.cut.cut_id : `missing-${row.title}`}>
            <section aria-label={row.title}>
              <h3 className="mb-2 flex flex-wrap items-baseline gap-x-2 text-sm font-semibold text-foreground">
                <span className="tabular-nums text-muted-foreground">{i + 1}.</span>
                <span>{row.title}</span>
                {optional && (
                  <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
                    optional
                  </span>
                )}
                {duration && <span className="text-xs font-normal text-muted-foreground">{duration}</span>}
                {row.kind === 'video' && row.cut.video_url && row.cut.video_viewer_url && (
                  <a
                    href={withBase(row.cut.video_viewer_url)}
                    className="ml-auto text-xs font-normal text-primary hover:underline"
                  >
                    Open this cut <span aria-hidden>→</span>
                  </a>
                )}
              </h3>
              <div className="grid gap-4 md:grid-cols-[3fr_2fr]">
                <div className="overflow-hidden rounded-lg border border-input bg-black">
                  {row.kind === 'video' && row.cut.video_url ? (
                    <video
                      src={withBase(row.cut.video_url)}
                      controls
                      preload="metadata"
                      className="max-h-[50vh] w-full bg-black"
                    />
                  ) : (
                    <div className="flex h-40 items-center justify-center bg-card px-4 text-center text-sm text-muted-foreground">
                      {row.kind === 'missing'
                        ? 'Not rendered yet.'
                        : 'This cut is private: only members of its workspace can watch it.'}
                    </div>
                  )}
                </div>
                <div className="space-y-2 text-sm text-foreground-secondary">
                  {row.words.length > 0 ? (
                    row.words.map((s) => <p key={s.id}>{s.text}</p>)
                  ) : (
                    <p className="text-muted-foreground">No narration matches this cut's scenes.</p>
                  )}
                </div>
              </div>
            </section>
          </li>
        )
      })}
    </ol>
  )
}
