import { useEffect, useState, type JSX } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import { getRunner } from '@/api/harness'
import { WorkbenchSkeleton } from 'canopy-ui'
import { scopedPath } from '@/lib/scopedLinks'

// `/runners/:runnerId/:tab?` — a runner's page for a caller that knows the runner
// but not its workspace: the Canopy Runner app's "Settings…" item opens
// `/runners/<its id>/settings`. Forwards to `/w/<workspace>/runners/<id>/<tab>`;
// a runner the viewer cannot read lands on the supervisor, which lists theirs.
export function RunnerRedirect(): JSX.Element {
  const { runnerId = '', tab = 'overview' } = useParams()
  const [to, setTo] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    getRunner(runnerId)
      .then((r) => alive && setTo(r.workspace ? scopedPath(r.workspace, `/runners/${r.id}/${tab}`) : '/supervisor'))
      .catch(() => alive && setTo('/supervisor'))
    return () => {
      alive = false
    }
  }, [runnerId, tab])
  return to ? <Navigate to={to} replace /> : <div className="p-6"><WorkbenchSkeleton /></div>
}
