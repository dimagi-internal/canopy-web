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

  const items: NavItem[] = [
    // Inbox = OPEN items, decidable in place (the count). Items = the full ledger
    // incl. decided/dismissed + batch sittings (?batch=) — browse/history, no badge.
    { to: 'inbox', label: 'Inbox', count: waiting },
    { to: 'overview', label: 'Overview' },
    { to: 'tasks', label: 'Tasks', count: agent.task_count },
    { to: 'items', label: 'Items' },
    { to: 'turns', label: 'Turns', count: agent.turn_count },
    // No count: the agent detail carries no schedule_count, and a badge that
    // needs its own fetch to say "3" isn't worth a request on every rail render.
    { to: 'schedules', label: 'Schedules' },
    { to: 'syncs', label: 'Syncs', count: agent.sync_count },
    { to: 'work-products', label: 'Work products', count: agent.work_product_count },
    { to: 'skills', label: 'Skills', count: agent.skill_count },
  ]

  const header = (
    <div className="px-4 py-4">
      <Link to="/agents" className="text-[12px] text-muted-foreground hover:text-primary transition-colors">
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
      <nav className="px-2 py-3">
        <div className="flex gap-0.5 overflow-x-auto md:flex-col md:overflow-x-visible">
          {items.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === 'overview'} className="shrink-0 md:shrink">
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
