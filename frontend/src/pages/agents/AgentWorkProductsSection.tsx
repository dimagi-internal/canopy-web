import { useEffect, useState } from 'react'
import { useLocation, useOutletContext } from 'react-router-dom'

import {
  listAgentSyncs,
  listAgentWorkProducts,
  type AgentSyncOut,
  type AgentWorkProductOut,
} from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { SyncCard, WorkProductCard } from '@/components/agents/cards'
import { Section } from '@/pages/agents/sectionLayout'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

// WHAT THIS AGENT PRODUCED — the deliverables, and the periodic report on them.
//
// "Syncs" was its own rail entry and its own word. It is a manager sync: a
// period review the agent writes about its own work, with self-grades, posted
// as a doc. Nothing in "sync" says that — a sync could be a data sync, a
// calendar sync, a repo sync — so it is STATUS REPORTS now, and it sits beside
// the work it reports on rather than one rail entry away from it.
export function AgentWorkProductsSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [items, setItems] = useState<AgentWorkProductOut[] | null>(null)
  const [reports, setReports] = useState<AgentSyncOut[] | null>(null)

  useEffect(() => {
    let cancelled = false
    setItems(null)
    setReports(null)
    listAgentWorkProducts(agent.slug, { limit: 200 })
      .then((page) => !cancelled && setItems(page.items))
      .catch(() => !cancelled && setItems([]))
    listAgentSyncs(agent.slug, { limit: 200 })
      .then((page) => !cancelled && setReports(page.items))
      .catch(() => !cancelled && setReports([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  // `#status-reports` is the old Syncs rail entry's address now.
  const { hash } = useLocation()
  useEffect(() => {
    if (!hash) return
    document.getElementById(decodeURIComponent(hash.slice(1)))?.scrollIntoView?.({ block: 'start' })
  }, [hash, items, reports])

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Work products" count={items?.length} />

      <Section
        id="deliverables"
        title="Deliverables"
        description={`What ${agent.name} has produced — docs, decks, stories, links.`}
      >
        {items === null ? (
          <WorkbenchSkeleton />
        ) : items.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No work products yet.</p>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {items.map((wp) => (
              <WorkProductCard key={wp.id} wp={wp} />
            ))}
          </div>
        )}
      </Section>

      <Section
        id="status-reports"
        title="Status reports"
        description={`Periodic reviews ${agent.name} writes about its own work, with its own grades.`}
      >
        {reports === null ? (
          <WorkbenchSkeleton />
        ) : reports.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No status reports yet.</p>
        ) : (
          <div className="space-y-3">
            {reports.map((s) => (
              <SyncCard key={s.id} sync={s} />
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}
