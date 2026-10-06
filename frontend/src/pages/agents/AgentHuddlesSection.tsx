import { useEffect, useState } from 'react'
import { useOutletContext, useParams } from 'react-router-dom'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'
import { listHuddles, type HuddleSummary } from '@/api/huddles'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { HuddleList } from '@/pages/huddles/HuddleList'

/** The huddles this agent led or was in. The tasks they produced live on its
 * Work board, linked back to each huddle. */
export function AgentHuddlesSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const { workspace = '' } = useParams()
  const [rows, setRows] = useState<HuddleSummary[] | null>(null)

  useEffect(() => {
    let alive = true
    setRows(null)
    listHuddles({ agent: agent.slug, limit: 100 })
      .then((r) => alive && setRows(r))
      .catch(() => alive && setRows([]))
    return () => {
      alive = false
    }
  }, [agent.slug])

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Huddles" count={rows?.length} />
      {rows === null ? (
        <WorkbenchSkeleton />
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          {agent.slug} has not been in a huddle yet. A huddle is a team of agents syncing in rounds — what each is
          working on, then the work they agree to push forward together.
        </p>
      ) : (
        <HuddleList workspace={workspace} huddles={rows} />
      )}
    </div>
  )
}
