import type { JSX } from 'react'
import { NavLink, useParams } from 'react-router-dom'
import { clsx } from 'clsx'

/** The three views of one fleet: the map, and the two tables it is drawn from. */
const VIEWS = [
  { to: '', label: 'Map' },
  { to: '/runners', label: 'Runners' },
  { to: '/agents', label: 'Agent access' },
]

export function TopologyViews(): JSX.Element {
  const { workspace = '' } = useParams()
  const base = `/w/${workspace}/settings/topology`
  return (
    <nav aria-label="Topology views" className="mb-4 inline-flex gap-0.5 rounded-lg border border-border bg-card p-0.5">
      {VIEWS.map((v) => (
        <NavLink
          key={v.label}
          to={`${base}${v.to}`}
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
  )
}
