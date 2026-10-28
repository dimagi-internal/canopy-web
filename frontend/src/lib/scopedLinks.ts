import { useParams } from 'react-router-dom'

/**
 * Every page that shows tenant data lives under `/w/:workspace/` — the share
 * links too (owner decision, 2026-10-08; canopy-web#1289). A share token still
 * opens one artifact without a login; it just does so at its workspace's
 * address, and the page asks the API to confirm the row really lives there.
 *
 * There is ONE address per artifact and no flat form (canopy-web#1337): a flat
 * `/walkthrough/…`, `/review/…`, `/share/…`, `/storyboard/…`, `/narrative/…`
 * or `/ddd-release/…` link is a server 404, so a workspace is required here —
 * nothing falls back to a flat path.
 */

/** `path` under `workspace`: `/w/<ws><path>`. */
export function scopedPath(workspace: string, path: string): string {
  if (!workspace) throw new Error(`no workspace to address ${path} under`)
  return `/w/${encodeURIComponent(workspace)}${path}`
}

/**
 * The scoped public viewers — `/w/<ws>/walkthrough/<id>`, `/w/<ws>/review/<id>`,
 * `/w/<ws>/share/<token>`, `/w/<ws>/storyboard/<slug>`,
 * `/w/<ws>/narrative/<slug>`, `/w/<ws>/ddd-release/<narrative>/<run>` and a
 * workspace app's public pages, `/w/<ws>/app/<key>/…`. They self-gate (on their
 * token, or the app being public) like the flat routes did, so an anonymous
 * visitor is not bounced to login there. The trailing slash after the viewer's
 * name is load-bearing: `/w/<ws>/walkthroughs`, `/w/<ws>/storyboards` and
 * `/w/<ws>/apps` (the member pages) stay behind the gate. Mirrors
 * `_SCOPED_VIEWER` in apps/common/middleware.py.
 */
export const SCOPED_VIEWER_RE =
  /^\/w\/[^/]+\/(walkthrough|review|share|storyboard|narrative|ddd-release|app)\//

export function isScopedViewerPath(path: string): boolean {
  return SCOPED_VIEWER_RE.test(path)
}

/** `scopedPath` bound to the current route's `:workspace` — for links drawn on
 * a tenant page, which already knows where it is. */
export function useScopedPath(): (path: string) => string {
  const { workspace = '' } = useParams()
  return (path: string) => scopedPath(workspace, path)
}

/** A runner's one page: `/w/<its workspace>/runners/<id>` — the address every link
 *  to a box uses (alerts build the same one server-side). A runner with no
 *  workspace has no page and opens on /supervisor. */
export function runnerHref(runner: { id: string; workspace?: string | null }): string {
  return runner.workspace
    ? scopedPath(runner.workspace, `/runners/${runner.id}`)
    : `/supervisor?tab=runners&runner=${runner.id}`
}

/** A workspace vault's one page. */
export function vaultHref(workspace: string, vaultId: string): string {
  return scopedPath(workspace, `/vaults/${vaultId}`)
}

