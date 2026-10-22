import type { JSX } from 'react'
import { NavLink, useParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { scopedPath } from '../../lib/scopedLinks'

/** One dashboard: is everything set up right, how is it used, and who works with whom. */
const VIEWS = [
  { to: 'health', label: 'Health' },
  { to: 'analytics', label: 'Analytics' },
  { to: 'network', label: 'Network' },
]

export function HealthViews({ title }: { title: string }): JSX.Element {
  const { workspace = '' } = useParams()
  return (
    <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-lg font-semibold text-foreground">
        {title}
        <span className="text-muted-foreground"> · {workspace} and every workspace below it</span>
      </h1>
      <nav aria-label="Dashboard views" className="inline-flex gap-0.5 rounded-lg border border-border bg-card p-0.5">
        {VIEWS.map((v) => (
          <NavLink
            key={v.to}
            to={scopedPath(workspace, `/${v.to}`)}
            end
            className={({ isActive }) =>
              clsx(
                'inline-flex min-h-9 items-center rounded-md px-3 text-[13px]',
                isActive ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:text-foreground-secondary',
              )
            }
          >
            {v.label}
          </NavLink>
        ))}
      </nav>
    </header>
  )
}

/** Shown to a non-admin: every view is the admin tier, like the topology. */
export function AdminOnly({ what }: { what: string }): JSX.Element {
  return (
    <p className="text-[13px] text-muted-foreground" data-testid="health-forbidden">
      Only a workspace admin or owner can see {what}.
    </p>
  )
}
