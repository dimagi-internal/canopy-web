import type { Model } from './model'
import { fmtDay, skillsSeries, weeklyCounts } from './model'

const W = 880, AH = 190, BY = 200, BH = 70

export function SkillHistoryChart({ model, day, scope, scopeLabel, onDay }: {
  model: Model; day: number; scope: string[] | null; scopeLabel: string; onDay: (d: number) => void
}) {
  const N = Math.max(model.days, 1)
  const x = (d: number) => (d / N) * W
  const series = skillsSeries(model)
  const max = Math.max(...series, 1)
  const y = (v: number) => AH - (v / max) * 150
  const line = series.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const area = `M0,${AH} ${series.map((v, i) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} L${W},${AH} Z`
  const all = weeklyCounts(model, null)
  const sel = scope ? weeklyCounts(model, scope) : null
  const bmax = Math.max(...all, 1)
  const bw = W / all.length
  const bars = (w: number[]) => w.map((c, i) => {
    const h = c ? Math.max(1.5, (c / bmax) * BH) : 0
    return `M${(i * bw + 2).toFixed(1)},${(BY + BH - h).toFixed(1)} h${(bw - 4).toFixed(1)} v${h.toFixed(1)} h-${(bw - 4).toFixed(1)} Z`
  }).join(' ')
  const months: number[] = []
  for (let d = 0; d <= model.days; d++) if (fmtDay(model, d).endsWith(' 1')) months.push(d)
  const cx = x(day)

  return (
    <figure className="m-0 flex flex-col gap-1.5">
      <figcaption className="flex items-baseline justify-between text-[13px]">
        <span className="font-semibold text-foreground">Skills in the repo, and SKILL.md revisions per week
          <span className="font-normal text-muted-foreground"> · {scopeLabel}</span></span>
        <span className="text-[12px] text-muted-foreground">Click the chart to move to that date</span>
      </figcaption>
      <svg viewBox={`0 -10 ${W} 320`} className="w-full overflow-visible" role="img"
           aria-label={`Skills over time and revisions per week, ${scopeLabel}`}>
        <path d={area} className="fill-muted" />
        <path d={line} fill="none" className="stroke-primary" strokeWidth={2.5} />
        <path d={bars(all)} className="fill-muted-foreground/40" />
        {sel && <path d={bars(sel)} className="fill-primary" />}
        {months.map((d) => (
          <text key={d} x={x(d)} y={BY + BH + 24} className="fill-muted-foreground font-mono text-[12px]">{fmtDay(model, d).split(' ')[0]}</text>
        ))}
        <text x={W - 4} y={y(max) - 6} textAnchor="end" className="fill-muted-foreground font-mono text-[11px]">{max} skills</text>
        <line x1={cx} x2={cx} y1={-6} y2={BY + BH + 6} className="stroke-foreground" strokeWidth={1.5} />
        <rect x={0} y={-10} width={W} height={BY + BH + 20} fill="transparent" className="cursor-pointer"
              onClick={(e) => {
                const r = (e.currentTarget as SVGRectElement).getBoundingClientRect()
                onDay(Math.round(((e.clientX - r.left) / r.width) * model.days))
              }} />
      </svg>
    </figure>
  )
}
