import { useState, type JSX } from 'react'

import type { BrainOutput, BrainTargetScore } from '@/api/brain'

// Small pictures of how learning findings land, drawn with SVG and semantic token fills only (both themes come from the
// tokens): a finding's lineage strip and sightings sparkline (Findings tab) and a target's weekly status bars (the
// scorecard on the Setup tab). Each has a hover title per mark and an aria-label that says the same in words; the
// numbers they draw are also written beside them, so nothing is colour alone.

const DAY = 86_400_000

/** "finding → N sessions → M people": what a finding reaches across every repeat. The sessions node opens the
 *  evidence. */
export function LineageStrip({ o, onEvidence }: { o: BrainOutput; onEvidence?: () => void }): JSX.Element | null {
  const sessions = o.sessions_seen ?? 0
  const people = o.people_seen ?? 0
  if (!sessions) return null
  const node = 'rounded-md border border-border bg-background px-2 py-0.5 text-xs tabular-nums text-foreground'
  const link = <span aria-hidden="true" className="h-px w-4 shrink-0 bg-border" />
  return (
    <div className="flex flex-wrap items-center gap-1" role="group"
         aria-label={`Seen in ${sessions} session${sessions === 1 ? '' : 's'} with ${people} ${people === 1 ? 'person' : 'people'}`}>
      <span className={`${node} text-muted-foreground`}>This finding</span>
      {link}
      {onEvidence ? (
        <button type="button" className={`${node} underline-offset-2 hover:underline`} onClick={onEvidence}
                title="Show the evidence">
          {sessions} session{sessions === 1 ? '' : 's'}
        </button>
      ) : <span className={node}>{sessions} session{sessions === 1 ? '' : 's'}</span>}
      {link}
      <span className={node}>{people} {people === 1 ? 'person' : 'people'}</span>
      {o.breadth && (
        <span className="ml-1 text-xs text-muted-foreground">
          {o.breadth === 'pattern' ? 'a pattern' : 'a single session so far'}
        </span>
      )}
    </div>
  )
}

const SW = 160, SH = 20, R = 4

/** When a finding was seen: one dot per sighting on a line from the first sighting to now. */
export function Sightings({ times, now = Date.now() }: { times: readonly string[]; now?: number }): JSX.Element | null {
  const at = times.map((t) => Date.parse(t)).filter((t) => !Number.isNaN(t)).sort((a, b) => a - b)
  if (at.length === 0) return null
  const start = Math.min(at[0], now - 7 * DAY)
  const span = Math.max(now - start, DAY)
  const x = (t: number) => R + ((t - start) / span) * (SW - 2 * R)
  const day = (t: number) => new Date(t).toISOString().slice(0, 10)
  return (
    <svg width={SW} height={SH} viewBox={`0 0 ${SW} ${SH}`} role="img" className="shrink-0"
         aria-label={`Seen ${at.length} time${at.length === 1 ? '' : 's'}, first ${day(at[0])}, last ${day(at[at.length - 1])}`}>
      <line x1={R} x2={SW - R} y1={SH / 2} y2={SH / 2} className="stroke-border" strokeWidth={1} />
      <line x1={SW - R} x2={SW - R} y1={SH / 2 - 4} y2={SH / 2 + 4} className="stroke-border" strokeWidth={1} />
      {at.map((t, i) => (
        <g key={i}>
          {/* Hit target larger than the mark. */}
          <circle cx={x(t)} cy={SH / 2} r={R + 4} fill="transparent"><title>Seen {day(t)}</title></circle>
          <circle cx={x(t)} cy={SH / 2} r={R} className="fill-primary stroke-card" strokeWidth={1.5} pointerEvents="none" />
        </g>
      ))}
    </svg>
  )
}

/** The statuses a scorecard week stacks, bottom first, with the token each is drawn in (the Findings tab's tones). */
export const SCORE_LAYERS = [
  { key: 'done', label: 'Done', className: 'fill-success' },
  { key: 'accepted', label: 'Accepted', className: 'fill-special' },
  { key: 'open', label: 'Open', className: 'fill-info' },
  { key: 'dismissed', label: 'Dismissed', className: 'fill-muted-foreground' },
] as const

const BW = 168, BH = 36

/** A target's findings by the week they were made, stacked by where each stands now. */
export function WeekBars({ score }: { score: BrainTargetScore }): JSX.Element {
  const [hover, setHover] = useState<number | null>(null)
  const weeks = score.weeks
  const total = (w: (typeof weeks)[number]) => SCORE_LAYERS.reduce((n, l) => n + (w[l.key] ?? 0), 0)
  const max = Math.max(1, ...weeks.map(total))
  const step = BW / Math.max(weeks.length, 1)
  const gap = 2
  const words = (w: (typeof weeks)[number]) =>
    `week of ${w.week}: ${SCORE_LAYERS.map((l) => `${l.label.toLowerCase()} ${w[l.key] ?? 0}`).join(', ')}`
  return (
    <figure className="m-0 inline-flex flex-col gap-0.5">
      <svg width={BW} height={BH} viewBox={`0 0 ${BW} ${BH}`} role="img" onMouseLeave={() => setHover(null)}
           aria-label={`${score.label}: findings made in each of the last ${weeks.length} weeks, by status`}>
        <line x1={0} x2={BW} y1={BH - 0.5} y2={BH - 0.5} className="stroke-border" />
        {weeks.map((w, i) => {
          let base = BH - 1
          return (
            <g key={w.week} onMouseEnter={() => setHover(i)}>
              <rect x={i * step} y={0} width={step} height={BH} fill="transparent"><title>{words(w)}</title></rect>
              {SCORE_LAYERS.map((l) => {
                const h = ((w[l.key] ?? 0) / max) * (BH - 2)
                if (!h) return null
                base -= h
                return (
                  <rect key={l.key} x={i * step + gap / 2} y={base} width={Math.max(step - gap, 1)}
                        height={Math.max(h - 1, 1)} rx={1.5} className={l.className} pointerEvents="none"
                        opacity={hover === null || hover === i ? 1 : 0.55} />
                )
              })}
            </g>
          )
        })}
      </svg>
      <figcaption className="text-[11px] tabular-nums text-muted-foreground">
        {hover === null ? `${weeks.reduce((n, w) => n + total(w), 0)} in ${weeks.length} weeks` : words(weeks[hover])}
      </figcaption>
    </figure>
  )
}

/** The scorecard's legend: which fill is which status. */
export function ScoreLegend(): JSX.Element {
  return (
    <span className="flex flex-wrap gap-3 text-[11px] text-muted-foreground">
      {SCORE_LAYERS.map((l) => (
        <span key={l.key} className="inline-flex items-center gap-1">
          <svg width="8" height="8" aria-hidden="true"><rect width="8" height="8" rx="2" className={l.className} /></svg>
          {l.label}
        </span>
      ))}
    </span>
  )
}
