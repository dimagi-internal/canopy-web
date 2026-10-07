import { useEffect, useState } from 'react'
import { useLocation, useOutletContext } from 'react-router-dom'
import { listAgentSyncs, listAgentTurns, type AgentSyncOut, type AgentTurnOut } from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { SyncCard, TurnCard } from '@/components/agents/cards'
import { Section } from '@/pages/agents/sectionLayout'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'

// Turns, and the periodic report on them.
//
// A "sync" is a manager sync: a period review the agent writes about its own
// work, with self-grades, posted as a doc. Nothing in "sync" says that — it
// could be a data sync, a calendar sync, a repo sync — so it is STATUS REPORTS,
// and it sits beside the turns it reports on. `#status-reports` is the old
// Syncs rail entry's address.
export function AgentTurnsSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [turns, setTurns] = useState<AgentTurnOut[] | null>(null)
  const [reports, setReports] = useState<AgentSyncOut[] | null>(null)

  useEffect(() => {
    let cancelled = false
    setTurns(null)
    setReports(null)
    listAgentTurns(agent.slug, { limit: 200 })
      .then((page) => !cancelled && setTurns(page.items))
      .catch(() => !cancelled && setTurns([]))
    listAgentSyncs(agent.slug, { limit: 200 })
      .then((page) => !cancelled && setReports(page.items))
      .catch(() => !cancelled && setReports([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  const { hash } = useLocation()
  useEffect(() => {
    if (!hash) return
    document.getElementById(decodeURIComponent(hash.slice(1)))?.scrollIntoView?.({ block: 'start' })
  }, [hash, turns, reports])

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Turns" count={turns?.length} />
      {turns === null ? (
        <WorkbenchSkeleton />
      ) : turns.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          No turns yet. A packaged turn records the request it advanced, what the agent did, the
          deliverables — and, optionally, a link to the session transcript.
        </p>
      ) : (
        <div className="space-y-3">
          {turns.map((t) => (
            <TurnCard key={t.id} turn={t} />
          ))}
        </div>
      )}

      <div className="mt-10">
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
    </div>
  )
}
