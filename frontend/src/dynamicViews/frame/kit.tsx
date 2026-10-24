/**
 * The components a dynamic view's code gets as globals (no imports — the code is
 * not a module). Small on purpose: canopy's look, dense tables, a number, a bar
 * chart. Named in the authoring guide (apps/dynamic_views/guide.py); keep the two
 * in step.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

type Tone = 'default' | 'success' | 'warning' | 'destructive'

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-foreground',
  success: 'text-success',
  warning: 'text-warning',
  destructive: 'text-destructive',
}

const TONE_BADGE: Record<Tone, string> = {
  default: 'bg-muted text-foreground',
  success: 'bg-success/15 text-success',
  warning: 'bg-warning/15 text-warning',
  destructive: 'bg-destructive/15 text-destructive',
}

/** How a `Link` (and a plain `<a href>`) leaves the frame: through the page. */
export const NavigateContext = createContext<(path: string) => void>(() => {})

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card text-card-foreground">
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Stat({ label, value, hint, tone = 'default' }: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${TONE_TEXT[tone] ?? TONE_TEXT.default}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

export function Badge({ tone = 'default', children }: { tone?: Tone; children?: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${TONE_BADGE[tone] ?? TONE_BADGE.default}`}>
      {children}
    </span>
  )
}

export interface Column<Row> {
  key: string
  label: ReactNode
  render?: (row: Row) => ReactNode
  align?: 'left' | 'right'
}

function cell(row: Record<string, unknown>, key: string): unknown {
  return key.split('.').reduce<unknown>((v, k) => (v && typeof v === 'object' ? (v as Record<string, unknown>)[k] : undefined), row)
}

function show(v: unknown): ReactNode {
  if (v == null) return ''
  if (typeof v === 'object') return Array.isArray(v) ? v.map(String).join(', ') : JSON.stringify(v)
  return String(v)
}

export function Table<Row extends Record<string, unknown>>({ rows, columns, empty = 'Nothing to show.' }: {
  rows: Row[] | null | undefined
  columns: Column<Row>[]
  empty?: ReactNode
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null)
  const list = useMemo(() => (Array.isArray(rows) ? rows : []), [rows])
  const sorted = useMemo(() => {
    if (!sort) return list
    return [...list].sort((a, b) => {
      const x = cell(a, sort.key)
      const y = cell(b, sort.key)
      if (x == null) return 1
      if (y == null) return -1
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sort.dir
    })
  }, [list, sort])
  if (list.length === 0) return <Empty>{empty}</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            {columns.map((c) => (
              <th key={c.key} className={`px-2 py-1.5 font-medium ${c.align === 'right' ? 'text-right' : ''}`}>
                <button
                  type="button"
                  className="hover:text-foreground"
                  onClick={() => setSort((s) => (s?.key === c.key ? { key: c.key, dir: s.dir === 1 ? -1 : 1 } : { key: c.key, dir: 1 }))}
                >
                  {c.label}
                  {sort?.key === c.key ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, i) => (
            <tr key={i} className="border-b border-border last:border-0">
              {columns.map((c) => (
                <td key={c.key} className={`px-2 py-1.5 align-top ${c.align === 'right' ? 'text-right tabular-nums' : ''}`}>
                  {c.render ? c.render(row) : show(cell(row, c.key))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function BarChart({ data, height }: { data: { label: ReactNode; value: number }[] | null | undefined; height?: number }) {
  const list = Array.isArray(data) ? data : []
  if (list.length === 0) return <Empty>No data.</Empty>
  const max = Math.max(...list.map((d) => Number(d.value) || 0), 1)
  return (
    <div className="flex flex-col gap-1.5" style={height ? { maxHeight: height, overflowY: 'auto' } : undefined}>
      {list.map((d, i) => (
        <div key={i} className="grid grid-cols-[minmax(6rem,12rem)_1fr_3rem] items-center gap-2 text-sm">
          <div className="truncate text-muted-foreground">{d.label}</div>
          <div className="h-3 rounded bg-muted">
            <div className="h-3 rounded bg-primary" style={{ width: `${((Number(d.value) || 0) / max) * 100}%` }} />
          </div>
          <div className="text-right tabular-nums">{d.value}</div>
        </div>
      ))}
    </div>
  )
}

export function Link({ to, children }: { to: string; children?: ReactNode }) {
  const navigate = useContext(NavigateContext)
  return (
    <a
      href={to}
      className="text-primary underline-offset-2 hover:underline"
      onClick={(e) => {
        e.preventDefault()
        navigate(to)
      }}
    >
      {children}
    </a>
  )
}

export function Empty({ children }: { children?: ReactNode }) {
  return <div className="py-3 text-sm text-muted-foreground">{children ?? 'Nothing here.'}</div>
}

export function Loading() {
  return <div className="py-3 text-sm text-muted-foreground">Loading…</div>
}

const GRID_COLS: Record<number, string> = {
  1: 'sm:grid-cols-1', 2: 'sm:grid-cols-2', 3: 'sm:grid-cols-3', 4: 'sm:grid-cols-4', 5: 'sm:grid-cols-5', 6: 'sm:grid-cols-6',
}
const GAPS: Record<number, string> = { 1: 'gap-1', 2: 'gap-2', 3: 'gap-3', 4: 'gap-4', 6: 'gap-6', 8: 'gap-8' }

export function Grid({ cols = 2, gap = 3, children }: { cols?: number; gap?: number; children?: ReactNode }) {
  return <div className={`grid grid-cols-1 ${GRID_COLS[cols] ?? GRID_COLS[2]} ${GAPS[gap] ?? GAPS[3]}`}>{children}</div>
}

export function Stack({ gap = 3, children }: { gap?: number; children?: ReactNode }) {
  return <div className={`flex flex-col ${GAPS[gap] ?? GAPS[3]}`}>{children}</div>
}

export const KIT = { Card, Stat, Badge, Table, BarChart, Link, Empty, Loading, Grid, Stack }
