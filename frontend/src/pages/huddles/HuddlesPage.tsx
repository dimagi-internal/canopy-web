import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import { listHuddles, type HuddleSummary } from '@/api/huddles'
import { HuddleList } from './HuddleList'

/**
 * Every huddle in this workspace, newest first. A huddle is a team of agents
 * syncing in rounds, led by one of them (canopy `huddle`); canopy-web stores
 * nothing for it — this list is derived from the leader's anchor turns.
 */
export function HuddlesPage() {
  const { workspace = '' } = useParams()
  const [rows, setRows] = useState<HuddleSummary[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    listHuddles({ limit: 100 })
      .then((r) => alive && setRows(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [workspace])

  return (
    <div className="mx-auto max-w-5xl p-6">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Huddles</h1>
        <p className="text-sm text-muted-foreground">
          The fleet syncing as a team — what each agent is on, and the work they agree to push forward
        </p>
      </header>
      {error && <p className="text-sm text-destructive">Couldn’t load huddles: {error}</p>}
      {!error && rows === null && <WorkbenchSkeleton />}
      {rows !== null && rows.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No huddles yet. A team leader starts one with <code>canopy huddle plan</code>; its rounds and the
          work it files show up here as they happen.
        </p>
      )}
      {rows !== null && rows.length > 0 && <HuddleList workspace={workspace} huddles={rows} />}
    </div>
  )
}

export default HuddlesPage
