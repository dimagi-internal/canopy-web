import type { ComponentProps } from 'react'

/**
 * The convention for dense app tables ("tables not cards", docs/architecture/frontend.md § Tables).
 * A header cell is `<Th>`, a body cell `<Td>`, a row `<Tr>`; their padding, weight, tone, alignment
 * and row rules live here, so `<td className="py-1.5 pr-3 text-muted-foreground">` is not repeated per
 * file (`dataTableConvention.test.ts` keeps raw copies of these from coming back).
 *
 * Density is the cell padding, shared by Th and Td so a column lines up (see `PAD_CLASS`):
 * `none`, `hair` (py-0.5), `compact` (py-1, the default for in-card lists), `compact-gap` (+ pr-3),
 * `compact-gutter` (+ pr-2), `dense` (px-2 py-1), `cozy` (py-1.5), `cozy-gap`, `cozy-gutter`,
 * `narrow` (px-1 py-1.5), `tight` (px-2 py-1.5, inline in a panel), `snug` (px-3 py-1.5),
 * `roomy` (px-3 py-2, a table that is the page's main content), `roomy-gap` (py-2 pr-3),
 * `roomy-gutter` (py-2 pr-4), `flush` (py-0.5 pr-3, a key/value list).
 *
 * Built on semantic tokens only: a header's colour comes from the surrounding table's text classes,
 * a body cell's from `tone`.
 */
export type TableDensity =
  | 'none'
  | 'hair'
  | 'compact'
  | 'compact-gap'
  | 'compact-gutter'
  | 'dense'
  | 'cozy'
  | 'cozy-gap'
  | 'cozy-gutter'
  | 'narrow'
  | 'tight'
  | 'snug'
  | 'roomy'
  | 'roomy-gap'
  | 'roomy-gutter'
  | 'flush'

export const PAD_CLASS: Record<TableDensity, string> = {
  none: '',
  hair: 'py-0.5',
  compact: 'py-1',
  'compact-gap': 'py-1 pr-3',
  'compact-gutter': 'py-1 pr-2',
  dense: 'px-2 py-1',
  cozy: 'py-1.5',
  'cozy-gap': 'py-1.5 pr-3',
  'cozy-gutter': 'py-1.5 pr-2',
  narrow: 'px-1 py-1.5',
  tight: 'px-2 py-1.5',
  snug: 'px-3 py-1.5',
  roomy: 'px-3 py-2',
  'roomy-gap': 'py-2 pr-3',
  'roomy-gutter': 'py-2 pr-4',
  flush: 'py-0.5 pr-3',
}

/** A header cell's classes per density (padding plus the default `font-medium`). */
export const TH_CLASS: Record<TableDensity, string> = Object.fromEntries(
  Object.entries(PAD_CLASS).map(([k, v]) => [k, `${v} font-medium`.trim()]),
) as Record<TableDensity, string>

export type CellAlign = 'left' | 'right' | 'center'
/** Below this breakpoint the cell (and its column's header) is hidden. */
export type HideBelow = 'sm' | 'md' | 'lg' | 'xl'

const ALIGN_CLASS: Record<CellAlign, string> = { left: 'text-left', right: 'text-right', center: 'text-center' }
// Literal strings so Tailwind sees every class.
const HIDE_CLASS: Record<HideBelow, string> = {
  sm: 'hidden sm:table-cell',
  md: 'hidden md:table-cell',
  lg: 'hidden lg:table-cell',
  xl: 'hidden xl:table-cell',
}

function join(...parts: Array<string | false | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

type ThProps = ComponentProps<'th'> & {
  density?: TableDensity
  /** `normal` for a quiet header; `inherit` leaves the weight to the table. Default is `font-medium`. */
  weight?: 'normal' | 'inherit'
  align?: CellAlign
  hideBelow?: HideBelow
  /** A header read by screen readers only (an actions or priority column). */
  srOnly?: boolean
  /** A small uppercase label header (`text-[11px] uppercase tracking-wide`, muted). */
  label?: boolean
}

/** A dense header cell. `className` is appended after the convention's classes. */
export function Th({ density = 'compact', weight, align, hideBelow, srOnly, label, className, ...props }: ThProps) {
  const cls = join(
    PAD_CLASS[density],
    weight === 'normal' ? 'font-normal' : weight === 'inherit' ? undefined : 'font-medium',
    align && ALIGN_CLASS[align],
    label && 'text-[11px] uppercase tracking-wide text-muted-foreground',
    hideBelow && HIDE_CLASS[hideBelow],
    srOnly && 'sr-only',
    className,
  )
  return <th className={cls || undefined} {...props} />
}

export type CellTone = 'foreground' | 'secondary' | 'muted'
const TONE_CLASS: Record<CellTone, string> = {
  foreground: 'text-foreground',
  secondary: 'text-foreground-secondary',
  muted: 'text-muted-foreground',
}

type TdProps = ComponentProps<'td'> & {
  density?: TableDensity
  tone?: CellTone
  align?: CellAlign
  hideBelow?: HideBelow
  /** `whitespace-nowrap` */
  nowrap?: boolean
  /** `tabular-nums`, for figures that should line up */
  numeric?: boolean
  /** `font-mono`, for ids, keys and paths */
  mono?: boolean
}

/** A dense body cell. `className` is appended after the convention's classes. */
export function Td({ density = 'compact', tone, align, hideBelow, nowrap, numeric, mono, className, ...props }: TdProps) {
  const cls = join(
    nowrap && 'whitespace-nowrap',
    PAD_CLASS[density],
    mono && 'font-mono',
    tone && TONE_CLASS[tone],
    align && ALIGN_CLASS[align],
    numeric && 'tabular-nums',
    hideBelow && HIDE_CLASS[hideBelow],
    className,
  )
  return <td className={cls || undefined} {...props} />
}

/** The rule between rows. `bottom-last` drops it on the last row, `top` is a row that draws its own top edge. */
export type RowRule = 'bottom' | 'bottom-last' | 'bottom-soft' | 'top'
const RULE_CLASS: Record<RowRule, string> = {
  bottom: 'border-b border-border',
  'bottom-last': 'border-b border-border last:border-0',
  'bottom-soft': 'border-b border-border/50',
  top: 'border-t border-border',
}

/** A header row's text: `plain`, `xs` (text-xs), `small` (11px), `label` (11px uppercase). */
export type RowHead = 'plain' | 'xs' | 'small' | 'label'
const HEAD_CLASS: Record<RowHead, string> = {
  plain: 'text-left text-muted-foreground',
  xs: 'text-left text-xs text-muted-foreground',
  small: 'text-left text-[11px] text-muted-foreground',
  label: 'text-left text-[11px] uppercase tracking-wide text-muted-foreground',
}

type TrProps = ComponentProps<'tr'> & {
  rule?: RowRule
  head?: RowHead
  /** `align-top`, for rows whose cells wrap to different heights */
  top?: boolean
}

/** A table row. `className` is appended after the convention's classes. */
export function Tr({ rule, head, top, className, ...props }: TrProps) {
  const cls = join(rule && RULE_CLASS[rule], head && HEAD_CLASS[head], top && 'align-top', className)
  return <tr className={cls || undefined} {...props} />
}
