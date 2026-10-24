// A project's scope against what is done, week by week (`progress.scope_history`,
// oldest first): two lines on one axis, so a project whose scope climbs faster
// than its done line is visibly not converging. Inline SVG drawn in
// `currentColor`, coloured by semantic text-* classes, with a title and a
// description for a screen reader.
import type { JSX } from 'react'

export interface ScopePoint {
  at: string
  scope: number
  done: number
}

/** The rows the API sends, as typed points (it types them as open objects). */
export function scopePoints(history: readonly Record<string, unknown>[] | undefined | null): ScopePoint[] {
  return (history ?? []).map((h) => ({
    at: String(h.at ?? ''),
    scope: Number(h.scope ?? 0),
    done: Number(h.done ?? 0),
  }))
}

const W = 560
const H = 140
const PAD = { top: 10, right: 12, bottom: 20, left: 28 }

function shortDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function ScopeChart({ points }: { points: readonly ScopePoint[] }): JSX.Element {
  if (points.length < 2) {
    return <p className="text-[13px] text-muted-foreground">Not enough history yet — the graph starts after its first week.</p>
  }
  const max = Math.max(1, ...points.map((p) => Math.max(p.scope, p.done)))
  const iw = W - PAD.left - PAD.right
  const ih = H - PAD.top - PAD.bottom
  const x = (i: number) => PAD.left + (i / (points.length - 1)) * iw
  const y = (v: number) => PAD.top + ih - (v / max) * ih
  const line = (key: 'scope' | 'done') => points.map((p, i) => `${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ')
  const first = points[0]
  const last = points[points.length - 1]
  const desc =
    `From ${shortDay(first.at)} to ${shortDay(last.at)}, scope went from ${first.scope} to ${last.scope} tasks ` +
    `and done from ${first.done} to ${last.done}.`
  return (
    <figure className="m-0" data-testid="scope-chart">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-labelledby="scope-chart-title scope-chart-desc"
        className="h-auto w-full max-w-2xl text-[10px]"
      >
        <title id="scope-chart-title">Scope versus done, by week</title>
        <desc id="scope-chart-desc">{desc}</desc>
        <g className="text-border">
          <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} stroke="currentColor" />
          <line x1={PAD.left} x2={W - PAD.right} y1={y(max)} y2={y(max)} stroke="currentColor" strokeDasharray="2 3" />
        </g>
        <g className="fill-current text-muted-foreground">
          <text x={PAD.left - 6} y={y(max) + 3} textAnchor="end">
            {max}
          </text>
          <text x={PAD.left - 6} y={y(0) + 3} textAnchor="end">
            0
          </text>
          <text x={x(0)} y={H - 4} textAnchor="start">
            {shortDay(first.at)}
          </text>
          <text x={x(points.length - 1)} y={H - 4} textAnchor="end">
            {shortDay(last.at)}
          </text>
        </g>
        <polyline points={line('scope')} fill="none" stroke="currentColor" strokeWidth={2} className="text-muted-foreground" />
        <polyline points={line('done')} fill="none" stroke="currentColor" strokeWidth={2} className="text-success" />
      </svg>
      <figcaption className="mt-1 flex gap-4 text-[12px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-muted-foreground" /> Scope {last.scope}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-success" /> Done {last.done}
        </span>
      </figcaption>
    </figure>
  )
}
