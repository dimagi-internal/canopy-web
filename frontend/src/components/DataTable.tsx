import type { ComponentProps } from 'react'

/**
 * The convention for dense app tables ("tables not cards", docs/architecture/frontend.md).
 * A header cell is `<Th density="…">`; the density is the one place its padding and weight
 * live, so a hand-written `<th className="py-1 font-medium">` is not repeated per file
 * (`dataTableConvention.test.ts` keeps the count of those from growing).
 *
 * - `compact`     py-1 rows, the default for in-card lists
 * - `compact-gap` compact with a right gutter between columns
 * - `roomy`       px-3 py-2, a table that is the page's main content
 * - `tight`       px-2 py-1.5, an inline table inside a panel
 *
 * Built on semantic tokens only: the colour comes from the surrounding table's text classes.
 */
export type TableDensity = 'compact' | 'compact-gap' | 'roomy' | 'tight'

export const TH_CLASS: Record<TableDensity, string> = {
  compact: 'py-1 font-medium',
  'compact-gap': 'py-1 pr-3 font-medium',
  roomy: 'px-3 py-2 font-medium',
  tight: 'px-2 py-1.5 font-medium',
}

type ThProps = ComponentProps<'th'> & { density?: TableDensity }

/** A dense header cell. `className` is appended after the density's classes. */
export function Th({ density = 'compact', className, ...props }: ThProps) {
  const cls = className ? `${TH_CLASS[density]} ${className}` : TH_CLASS[density]
  return <th className={cls} {...props} />
}
