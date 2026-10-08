import { useEffect, useState, type ReactNode } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import { getWalkthrough } from '../api/walkthroughs'
import { getReview } from '../api/reviews'
import { getShared } from '../api/sessions'
import { scopedPath } from '../lib/scopedLinks'

export type ArtifactKind = 'walkthrough' | 'review' | 'share'

/** Where the row lives, asked the way the old link's page asked: the same
 * token-gated read, so this learns nothing the page would not have shown. */
async function workspaceOf(kind: ArtifactKind, key: string, token: string | null) {
  if (kind === 'walkthrough') return (await getWalkthrough(key, token)).workspace ?? null
  if (kind === 'review') return (await getReview(key, token)).workspace ?? null
  return (await getShared(key)).workspace ?? null
}

/**
 * A flat `/walkthrough/:id`, `/review/:id` or `/share/:token` link — every one
 * sent before these pages moved under `/w/:workspace/` (canopy-web#1289).
 * Links already sent must keep working, so this reads the row's workspace and
 * replaces the URL with the scoped one, keeping the query (`?t=`) and the hash
 * (`#t=` / a scene).
 *
 * When the workspace cannot be resolved — the reader may not open the row, it
 * does not exist, or it was never homed — the page renders here in place, and
 * says what it would have said at the old address (not found, sign in, …).
 */
export function FlatArtifactRedirect({ kind, children }: { kind: ArtifactKind; children: ReactNode }) {
  const params = useParams()
  const key = (kind === 'share' ? params.token : params.id) ?? ''
  const { search, hash } = useLocation()
  const [target, setTarget] = useState<string | null | undefined>(undefined)

  useEffect(() => {
    let live = true
    const token = new URLSearchParams(search).get('t')
    workspaceOf(kind, key, token)
      .then((ws) => live && setTarget(ws))
      .catch(() => live && setTarget(null))
    return () => {
      live = false
    }
  }, [kind, key, search])

  if (target === undefined) return null
  if (target === null) return <>{children}</>
  return <Navigate to={`${scopedPath(target, `/${kind}/${key}`)}${search}${hash}`} replace />
}
