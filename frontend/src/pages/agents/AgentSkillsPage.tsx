import type { JSX } from 'react'
import { Outlet, useOutletContext } from 'react-router-dom'
import { ObjectTabs } from '@/components/objectPage/ObjectTabs'

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
      <ObjectTabs label="Skills views" tabs={TABS} className="px-6 pt-6" />
      <Outlet context={ctx} />
    </div>
  )
}
