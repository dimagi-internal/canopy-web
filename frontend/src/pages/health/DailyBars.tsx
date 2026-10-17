import { useState, type JSX } from 'react'

import type { AnalyticsDayOut } from '@/api/workspaces'

/** One stacked layer of a day's column. `className` is a token fill
 *  (`fill-primary`, `fill-success`…), never a raw palette colour. */
export interface BarLayer {
  key: keyof AnalyticsDayOut
  label: string
  className: string
}

const W = 600, H = 120, PAD = 18

/** Daily columns: one series, or a few stacked status layers. Hovering a day shows
 *  its values; a legend names the layers when there is more than one. */
export function DailyBars({ title, days, layers, format = (n) => n.toLocaleString() }: {
  title: string
  days: readonly AnalyticsDayOut[]
  layers: BarLayer[]
  format?: (n: number) => string
}): JSX.Element {
  const [hover, setHover] = useState<number | null>(null)
  const totals = days.map((d) => layers.reduce((n, l) => n + Number(d[l.key] ?? 0), 0))
  const max = Math.max(...totals, 0)
  const bw = W / Math.max(days.length, 1)
  const gap = Math.min(2, bw / 4)
  const y = (v: number) => (max ? (v / max) * (H - PAD) : 0)
  const shown = hover ?? days.length - 1
  const day = days[shown]

  return (
    <figure className="m-0 rounded-lg border border-border bg-card p-3">
      <figcaption className="mb-1 flex items-baseline justify-between gap-2 text-[12px]">
        <span className="font-semibold text-foreground">{title}</span>
        {day && (
          <span className="text-muted-foreground tabular-nums">
            {day.day}: {layers.length === 1 ? format(totals[shown])
              : layers.map((l) => `${l.label} ${format(Number(day[l.key] ?? 0))}`).join(' · ')}
          </span>
        )}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-28 w-full" role="img" preserveAspectRatio="none"
           aria-label={`${title}, ${days.length} days, peak ${format(max)}`}
           onMouseLeave={() => setHover(null)}>
        <line x1={0} x2={W} y1={H - 0.5} y2={H - 0.5} className="stroke-border" />
        {days.map((d, i) => {
          let base = H
          return (
            <g key={d.day} onMouseEnter={() => setHover(i)}>
              {/* Hit target: the whole column, not just the mark. */}
              <rect x={i * bw} y={0} width={bw} height={H} fill="transparent" />
              {layers.map((l) => {
                const h = y(Number(d[l.key] ?? 0))
                if (!h) return null
                base -= h
                return (
                  <rect key={l.key} x={i * bw + gap / 2} y={base} width={Math.max(bw - gap, 1)} height={Math.max(h - (layers.length > 1 ? 1 : 0), 1)}
                        rx={1.5} className={l.className} opacity={hover === null || hover === i ? 1 : 0.55} />
                )
              })}
            </g>
          )
        })}
      </svg>
      <div className="mt-1 flex justify-between text-[11px] text-muted-foreground tabular-nums">
        <span>{days[0]?.day}</span>
        {layers.length > 1 && (
          <span className="flex gap-3">
            {layers.map((l) => (
              <span key={l.key} className="inline-flex items-center gap-1">
                <svg width="8" height="8" aria-hidden="true"><rect width="8" height="8" rx="2" className={l.className} /></svg>
                {l.label}
              </span>
            ))}
          </span>
        )}
        <span>peak {format(max)}</span>
      </div>
    </figure>
  )
}
