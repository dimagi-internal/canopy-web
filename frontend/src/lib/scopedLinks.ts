import { useParams } from 'react-router-dom'

/**
 * Every page that shows tenant data lives under `/w/:workspace/` — the share
 * links too (owner decision, 2026-10-08; canopy-web#1289). A share token still
 * opens one artifact without a login; it just does so at its workspace's
 * address, and the page asks the API to confirm the row really lives there.
 */

/** `path` under `workspace` (`/w/<ws><path>`); flat when there is none. */
export function scopedPath(workspace: string | null | undefined, path: string): string {
  return workspace ? `/w/${encodeURIComponent(workspace)}${path}` : path
}

/**
 * The scoped public viewers — `/w/<ws>/walkthrough/<id>`, `/w/<ws>/review/<id>`
 * and `/w/<ws>/share/<token>`. They self-gate on their token like the flat
 * routes did, so an anonymous visitor is not bounced to login there. The
 * trailing slash after the viewer's name is load-bearing: `/w/<ws>/walkthroughs`
 * (the list) stays behind the gate. Mirrors `_SCOPED_VIEWER` in
 * apps/common/middleware.py.
 */
export const SCOPED_VIEWER_RE = /^\/w\/[^/]+\/(walkthrough|review|share)\//

export function isScopedViewerPath(path: string): boolean {
  return SCOPED_VIEWER_RE.test(path)
}

/** `scopedPath` bound to the current route's `:workspace` — for links drawn on
 * a tenant page, which already knows where it is. */
export function useScopedPath(): (path: string) => string {
  const { workspace } = useParams()
  return (path: string) => scopedPath(workspace, path)
}
