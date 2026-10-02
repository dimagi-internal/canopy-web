import type { JSX } from 'react'
import { NavLink, Outlet, useOutletContext } from 'react-router-dom'

import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'

// Skills and their history are one subject with two views, not two rail
// entries. History IS skill history — it reads every SKILL.md's git log — so it
// was a rail item whose name said nothing about skills, sitting a click away
// from the catalog it describes.
const TABS = [
  { to: '.', end: true, label: 'Catalog' },
  { to: 'history', end: false, label: 'How they changed' },
]

export function AgentSkillsPage(): JSX.Element {
  // A nested <Outlet /> does not inherit its parent's context — it must be
  // handed on, or both tabs crash destructuring `agent` from undefined.
  const ctx = useOutletContext<AgentOutletContext>()
  return (
    <div>
      <nav aria-label="Skills views" className="flex gap-1 border-b border-border px-6 pt-6">
        {TABS.map((t) => (
          <NavLink
            key={t.label}
            to={t.to}
            end={t.end}
            className={({ isActive }) =>
              `-mb-px border-b-2 px-3 py-2 text-[13px] ${
                isActive
                  ? 'border-primary text-foreground'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`
            }
          >
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={ctx} />
    </div>
  )
}
