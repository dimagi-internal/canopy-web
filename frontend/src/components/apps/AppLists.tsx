import type { JSX, ReactNode } from 'react'
import { Link } from 'react-router-dom'

/**
 * What the Apps page draws, for both of its tabs: the apps you have, and the store they come from.
 * One structure, whatever kind of app it holds.
 */

export function AppListHeading({ children }: { children: ReactNode }): JSX.Element {
  return <h2 className="mb-2 mt-8 text-sm font-semibold uppercase tracking-wide text-muted-foreground">{children}</h2>
}

export function InstalledAppList({ loading, empty, children }: { loading: boolean; empty: ReactNode; children: ReactNode[] }): JSX.Element {
  if (loading) return <div className="text-sm text-muted-foreground">Loading…</div>
  if (children.length === 0) return <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">{empty}</div>
  return <div className="divide-y divide-border rounded-lg border border-border bg-card" data-testid="app-installed-list">{children}</div>
}

export function InstalledAppRow({
  to,
  name,
  meta,
  badges,
  description,
  children,
  actions,
}: {
  to: string
  name: string
  meta: ReactNode
  badges?: ReactNode
  description: ReactNode
  /** Anything more to say about this one, below its description. */
  children?: ReactNode
  actions?: ReactNode
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 p-4" data-testid="app-installed-row">
      <div className="min-w-0 max-w-prose">
        <Link className="font-medium hover:underline" to={to}>{name}</Link>
        <span className="ml-2 text-xs text-muted-foreground">{meta}</span>
        {badges}
        <p className="mt-1 text-sm text-muted-foreground">{description}</p>
        {children}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-3 text-sm">{actions}</div>}
    </div>
  )
}

export function StoreAppGrid({ children }: { children: ReactNode }): JSX.Element {
  return <div className="grid gap-3 sm:grid-cols-2" data-testid="app-store-grid">{children}</div>
}

export function StoreAppCard({
  to,
  name,
  meta,
  badge,
  summary,
  cover,
  detailsLabel,
  action,
}: {
  to: string
  name: string
  meta: ReactNode
  badge?: ReactNode
  summary: ReactNode
  /** A screenshot's address, when the app has one. */
  cover?: string
  detailsLabel: string
  action: ReactNode
}): JSX.Element {
  return (
    <div className="flex flex-col justify-between gap-3 overflow-hidden rounded-lg border border-border bg-card" data-testid="app-store-card">
      <Link to={to} className="group block">
        {cover && <img src={cover} alt="" loading="lazy" className="block h-40 w-full border-b border-border object-cover object-top" />}
        <div className="p-4 pb-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium group-hover:underline">{name}</span>
            {badge}
          </div>
          <div className="text-xs text-muted-foreground">{meta}</div>
          <p className="mt-2 text-sm text-muted-foreground">{summary}</p>
        </div>
      </Link>
      <div className="flex items-center gap-3 p-4 pt-0">
        <Link to={to} className="text-sm text-primary hover:underline">{detailsLabel}</Link>
        {action}
      </div>
    </div>
  )
}

/** The store's primary action, on a button or a link. */
export const STORE_ACTION_CLASS = 'rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-75'
