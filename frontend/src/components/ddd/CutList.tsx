import { useState } from 'react'
import { withBase } from '@/lib/basePath'

/** One cut's pinned video, as either surface receives it. The narrative page
 *  and the review link carry the same fields under slightly different names, so
 *  each maps onto this before rendering (canopy-web#1293). */
export interface CutVideo {
  cut_id: string
  title: string
  /** Narration item ids this cut plays, in order. */
  scene_ids: readonly string[]
  walkthrough_id: string
  /** Playable for this reader; null when the video is private to its workspace. */
  video_url: string | null
  /** The cut's own walkthrough page, when the reader may open it. */
  viewer_url?: string | null
  duration_sec?: number | null
}

/** A narration item, by what the Cuts view needs of it. */
export interface CutScene {
  id: string
  title: string
  narration: string
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
  if (!scenes.some((s) => cutLabelOf(s.title))) return [{ cut: null, scenes }]
  const groups: Array<{ cut: string | null; scenes: T[] }> = []
  for (const s of scenes) {
    const cut = cutLabelOf(s.title)
    const last = groups[groups.length - 1]
    if (last && last.cut === cut) last.scenes.push(s)
    else groups.push({ cut, scenes: [s] })
  }
  return groups
}

function cutLabelOf(title: string | undefined): string | null {
  const t = title ?? ''
  const i = t.indexOf(' — ')
  return i > 0 ? t.slice(0, i).trim() : null
}

/** A row of the Cuts view: a cut that has a video, or one the narration names
 *  that has not been rendered yet. */
export interface CutSlot {
  key: string
  title: string
  optional: boolean
  scenes: CutScene[]
  video: CutVideo | null
}

const OPTIONAL = /\boptional\b/i

/**
 * Every cut, in narration order. The pinned videos are the cuts that exist; the
 * narration's cut-prefixed scene titles name the cuts that SHOULD exist, so a
 * cut nobody has rendered still gets its row ("not rendered yet") instead of
 * silently going missing. A cut is optional when its label says so ("Cut 7 ·
 * Optional") — the recipe's own flag never reaches canopy-web.
 */
export function cutSlots(cuts: CutVideo[], scenes: CutScene[]): CutSlot[] {
  const index = new Map(scenes.map((s, i) => [s.id, i]))
  const byId = new Map(scenes.map((s) => [s.id, s]))
  const covered = new Set(cuts.flatMap((c) => c.scene_ids))
  const placed: Array<{ pos: number; slot: CutSlot }> = []

  cuts.forEach((c, order) => {
    const words = c.scene_ids.map((id) => byId.get(id)).filter((s): s is CutScene => !!s)
    const positions = c.scene_ids.map((id) => index.get(id)).filter((p): p is number => p != null)
    const label = words.length > 0 ? cutLabelOf(words[0].title) : null
    // An upload with no title of its own is labelled by its id; the narration's
    // own label reads better.
    const title = c.title && c.title !== c.cut_id ? c.title : label || c.title || c.cut_id
    placed.push({
      pos: positions.length > 0 ? Math.min(...positions) : scenes.length + order,
      slot: {
        key: `cut:${c.cut_id || c.walkthrough_id}`,
        title,
        optional: OPTIONAL.test(title) || (label != null && OPTIONAL.test(label)),
        scenes: words,
        video: c,
      },
    })
  })

  for (const g of groupScenesByCut(scenes)) {
    if (!g.cut || g.scenes.some((s) => covered.has(s.id))) continue
    placed.push({
      pos: index.get(g.scenes[0].id) ?? scenes.length,
      slot: {
        key: `missing:${g.scenes[0].id}`,
        title: g.cut,
        optional: OPTIONAL.test(g.cut),
        scenes: g.scenes,
        video: null,
      },
    })
  }

  return placed.sort((a, b) => a.pos - b.pos).map((p) => p.slot)
}

export function formatDuration(sec: number | null | undefined): string | null {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return null
  const s = Math.round(sec)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const pathOf = (url: string) => url.split('?')[0]

/**
 * A recorded narrative's videos in one place: the hero on top when it is a
 * video of its own, then every cut in narration order, each beside the words it
 * speaks. Shared by the narrative page and the review link's Cuts tab, so the
 * two cannot drift (canopy-web#1293).
 */
export function CutList({
  cuts,
  scenes,
  hero,
}: {
  cuts: CutVideo[]
  scenes: CutScene[]
  /** The version's own video. Shown on top only when it is not one of the cuts. */
  hero?: { video_url: string | null; viewer_url?: string | null } | null
}) {
  const slots = cutSlots(cuts, scenes)
  const heroUrl = hero?.video_url ?? null
  const heroIsACut =
    heroUrl != null && cuts.some((c) => c.video_url && pathOf(c.video_url) === pathOf(heroUrl))
  const missing = slots.filter((s) => !s.video).length

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        {slots.length} short video{slots.length === 1 ? '' : 's'}, each beside what it says.
        {missing > 0 && ` ${missing} not rendered yet.`}
      </p>

      {heroUrl && !heroIsACut && (
        <section aria-label="Full video">
          <h3 className="mb-2 text-sm font-semibold text-foreground">Full video</h3>
          <video
            src={withBase(heroUrl)}
            controls
            preload="metadata"
            className="w-full max-h-[50vh] rounded-lg border border-input bg-black"
          />
        </section>
      )}

      <ol className="space-y-6">
        {slots.map((slot, i) => (
          <CutRow key={slot.key} slot={slot} number={i + 1} />
        ))}
      </ol>
    </div>
  )
}

function CutRow({ slot, number }: { slot: CutSlot; number: number }) {
  const v = slot.video
  // The upload's own duration when it sent one; else the player's, once loaded.
  const [loaded, setLoaded] = useState<number | null>(null)
  const duration = formatDuration(v?.duration_sec ?? loaded)
  const numbered = /^cut\s*\d/i.test(slot.title)

  return (
    <li>
      <section aria-label={slot.title} className="border-t border-border pt-4">
        <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 className="text-sm font-semibold text-foreground">
            {!numbered && <span className="mr-2 tabular-nums text-muted-foreground">{number}.</span>}
            {slot.title}
          </h3>
          {slot.optional && (
            <span className="rounded border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground">
              Optional
            </span>
          )}
          {duration && (
            <span className="text-xs tabular-nums text-muted-foreground" title="Length">
              {duration}
            </span>
          )}
          {v?.viewer_url && (
            <a
              href={withBase(v.viewer_url)}
              className="ml-auto text-xs text-primary hover:underline"
            >
              Open this video
            </a>
          )}
        </div>
        <div className="grid gap-4 md:grid-cols-[3fr_2fr]">
          {v?.video_url ? (
            <video
              src={withBase(v.video_url)}
              controls
              preload="metadata"
              onLoadedMetadata={(e) => setLoaded(e.currentTarget.duration)}
              className="w-full max-h-[50vh] rounded-lg border border-input bg-black"
            />
          ) : (
            <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-border bg-card px-4 text-center text-sm text-muted-foreground">
              {v ? 'This video is private: only members of its workspace can watch it.' : 'Not rendered yet.'}
            </div>
          )}
          <div className="space-y-2 text-sm leading-relaxed text-foreground-secondary">
            {slot.scenes.length > 0 ? (
              slot.scenes.map((s) => <p key={s.id}>{s.narration}</p>)
            ) : (
              <p className="text-muted-foreground">No narration matches this cut's scenes.</p>
            )}
          </div>
        </div>
      </section>
    </li>
  )
}
