import type { JSX } from 'react'
import { NavLink } from 'react-router-dom'
import { clsx } from 'clsx'

/** One tab: its own address (relative to the page), its label, and whether it
 *  needs attention (a setup step left, a failing credential) — a dot, so "what is
 *  left" shows from whichever tab you are on. */
export interface ObjectTab {
  to: string
  label: string
  dot?: boolean
  /** A dot's meaning, read aloud. */
  dotLabel?: string
  /** Active only on this exact address (an index tab whose siblings are below it). */
  end?: boolean
}

/**
 * THE tab row of every core object's page (an agent's settings, a workspace's, a
 * runner, a vault). Each tab is a route, so a deep link names a place and a tab
 * loads only its own data. One component, so the style is improved in one place.
 */
export function ObjectTabs({ label, tabs, className, relative = 'route' }: {
  /** What the tabs are, for a screen reader: "Runner", "Workspace settings". */
  label: string
  tabs: ObjectTab[]
  /** Spacing around the row; replaces the default bottom margin. */
  className?: string
  /** How each `to` resolves: against the page's route (a layout rendering its
   *  children, the usual case), or against the current address (a tab row inside
   *  one of those children, e.g. `../security`). */
  relative?: 'route' | 'path'
}): JSX.Element {
  return (
    <nav aria-label={label} className={clsx('flex gap-1 overflow-x-auto border-b border-border', className ?? 'mb-6')}>
      {tabs.map((t) => (
        <NavLink
          key={t.to}
          to={t.to}
          end={t.end}
          relative={relative}
          className={({ isActive }) =>
            clsx(
              'shrink-0 border-b-2 px-3 py-2 text-[13px] transition-colors',
              isActive
                ? 'border-primary font-medium text-foreground'
                : 'border-transparent text-muted-foreground hover:text-foreground-secondary',
            )
          }
        >
          {t.label}
          {t.dot && (
            <span className="ml-1.5 inline-block h-1.5 w-1.5 rounded-full bg-warning align-middle"
              aria-label={t.dotLabel ?? 'needs attention'} />
          )}
        </NavLink>
      ))}
    </nav>
  )
}
