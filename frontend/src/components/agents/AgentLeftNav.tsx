import { useEffect, useState } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { WorkbenchRail, WorkbenchNavItem } from 'canopy-ui'
import type { AgentDetailOut } from '@/api/agents'
import { listItems } from '@/api/items'

type NavItem = { to: string; label: string; count?: number }

export function AgentLeftNav({ agent }: { agent: AgentDetailOut }) {
  // The "N waiting on you" count for the inbox badge — the agent's open items.
  const [waiting, setWaiting] = useState<number | undefined>(undefined)
  useEffect(() => {
    let cancelled = false
    setWaiting(undefined)
    listItems(agent.slug, { state: 'open' })
      .then((rows) => !cancelled && setWaiting(rows.length))
      .catch(() => !cancelled && setWaiting(undefined))
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  // ONE badge, and it means "waiting on you".
  //
  // Six of these carried a size (Tasks 9, Turns 115, Skills 129) and four did
  // not, which made a blank badge unreadable: `Inbox 0` and `Syncs 0` render an
  // explicit zero, so blank could not mean "none" — it had to mean "unknown".
  // Credentials was the proof, blank in the rail while its own page header read
  // "Credentials 45" (a number the rail cannot reach without a fetch per render).
  //
  // Adding the missing counts would have made the rail consistent and still
  // useless: knowing there are 115 turns changes nothing about whether you click
  // Turns, and every section page already prints its own count in its header. So
  // the sizes go, blank becomes unambiguous everywhere, and the one number that
  // does change a decision keeps its badge. It also shortens every pill, which
  // is real estate the phone strip needs.
  const items: NavItem[] = [
    // Inbox = OPEN items, decidable in place. Items = the full ledger incl.
    // decided/dismissed + batch sittings (?batch=) — browse/history, no badge.
    { to: 'inbox', label: 'Inbox', count: waiting },
    { to: 'overview', label: 'Overview' },
    { to: 'tasks', label: 'Tasks' },
    { to: 'items', label: 'Items' },
    { to: 'turns', label: 'Turns' },
    { to: 'schedules', label: 'Schedules' },
    { to: 'syncs', label: 'Syncs' },
    { to: 'credentials', label: 'Credentials' },
    { to: 'work-products', label: 'Work products' },
    { to: 'skills', label: 'Skills' },
  ]

  const header = (
    <div className="px-4 py-4">
      <Link to="/agents" className="inline-flex min-h-11 items-center text-[12px] text-muted-foreground transition-colors hover:text-primary sm:min-h-0">
        ← Agents
      </Link>
      <div className="mt-3 flex items-start gap-3">
        {agent.avatar_url ? (
          <img
            src={agent.avatar_url}
            alt=""
            className="h-10 w-10 shrink-0 rounded-full border border-border object-cover"
          />
        ) : (
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground">
            {(agent.name || agent.slug).slice(0, 1).toUpperCase()}
          </span>
        )}
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold leading-snug text-foreground">{agent.name}</h2>
          {agent.email && (
            <a
              href={`mailto:${agent.email}`}
              className="block truncate text-[11px] text-muted-foreground hover:text-primary transition-colors"
            >
              {agent.email}
            </a>
          )}
        </div>
      </div>
    </div>
  )

  return (
    <WorkbenchRail header={header}>
      {/* Phone: a horizontal, scrollable strip — the same shape /supervisor already
          uses for its tabs. Stacked vertically it ran to nine rows, and with the
          identity header above it that pushed the actual content to 55% down the
          first screen: you scrolled past the navigation to reach what you opened.
          Desktop is unchanged from md up. */}
      {/* The strip scrolls, so it has to LOOK like it scrolls. Ten sections at
          375px means 588 of 899px sit off-screen (measured), and macOS hides
          overlay scrollbars — so Credentials, Skills, Turns, Schedules, Syncs and
          Work products were not merely off-screen but undiscoverable: a strip
          with no visible edge reads as "that is all of them". The right-edge
          fade is the affordance, and it is masked out from `md` up where the
          rail is vertical and everything is already visible. */}
      <nav className="relative px-2 py-3 after:pointer-events-none after:absolute after:inset-y-3 after:right-0 after:w-10 after:bg-gradient-to-l after:from-background after:via-background/80 after:to-transparent md:after:hidden">
        <div className="flex snap-x gap-0.5 overflow-x-auto md:flex-col md:overflow-x-visible">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === 'overview'}
              className="shrink-0 snap-start md:shrink md:snap-align-none"
            >
              {({ isActive }) => (
                <WorkbenchNavItem active={isActive} count={item.count}>
                  {item.label}
                </WorkbenchNavItem>
              )}
            </NavLink>
          ))}
        </div>
      </nav>
    </WorkbenchRail>
  )
}
