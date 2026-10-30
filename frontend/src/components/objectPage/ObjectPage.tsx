import type { JSX, ReactNode } from 'react'
import { ObjectTabs, type ObjectTab } from './ObjectTabs'

/**
 * The frame of a core object's page — a runner, a vault, a workspace's settings:
 * its name, one line saying what it is, a status, then its tabs, then the open
 * tab. Every core object has ONE such page at ONE address (docs/architecture/
 * pages.md § One editor per object); other surfaces link to it rather than
 * rebuilding its controls.
 *
 * No page padding: AppLayout's <main> already applies the gutter.
 */
export function ObjectPage({ title, context, status, subtitle, actions, tabs, tabsLabel, wide = false, children }: {
  title: ReactNode
  /** What kind of thing, or where it lives: "Runner", "dimagi". */
  context?: ReactNode
  /** A badge beside the name: "available", "no key yet". */
  status?: ReactNode
  subtitle?: ReactNode
  /** Links at the top right: "Live status →". */
  actions?: ReactNode
  /** Omitted for an object whose page is one scroll of collapsible sections. */
  tabs?: ObjectTab[]
  tabsLabel?: string
  /** A tab that is a full-width map or table (the workspace's Topology). */
  wide?: boolean
  children: ReactNode
}): JSX.Element {
  return (
    <div className={wide ? undefined : 'max-w-5xl'}>
      <header className="mb-5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-lg font-semibold text-foreground">
            {title}
            {context ? <span className="text-muted-foreground"> · {context}</span> : null}
          </h1>
          {status}
          {actions && <div className="ml-auto flex items-center gap-3 text-[12px]">{actions}</div>}
        </div>
        {subtitle && <p className="mt-1 text-[13px] text-foreground-secondary">{subtitle}</p>}
      </header>
      {tabs && tabsLabel && <ObjectTabs label={tabsLabel} tabs={tabs} />}
      {children}
    </div>
  )
}
