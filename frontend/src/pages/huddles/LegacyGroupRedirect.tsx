import { useEffect, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import { listCheckins } from '@/api/checkins'
import { scopedPath } from '../../lib/scopedLinks'

/**
 * /w/:workspace/huddles/groups/:slug — a work group's old page. Work groups
 * became project check-ins (2026-10-17): send the reader to the project the
 * group became (matched on the check-in's `legacy_slug`), or to the Huddles
 * page when there is no such check-in any more.
 */
export function LegacyGroupRedirect() {
  const { workspace = '', slug = '' } = useParams()
  const [to, setTo] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    listCheckins()
      .then((rows) => {
        const hit = rows.find((c) => c.legacy_slug === slug)
        if (alive) setTo(hit ? hit.url : scopedPath(workspace, '/huddles'))
      })
      .catch(() => alive && setTo(scopedPath(workspace, '/huddles')))
    return () => {
      alive = false
    }
  }, [workspace, slug])
  return to ? <Navigate to={to} replace /> : <div className="p-6"><WorkbenchSkeleton /></div>
}
