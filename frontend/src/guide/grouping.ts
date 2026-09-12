import { NAV_GROUPS } from '@/components/AppLayout/nav'
import { SURFACES, type SurfaceDescriptor } from './surfaces'

/**
 * Group descriptors by the header's existing nav groups, so the guide's shape
 * matches the menu people actually look at. Anything the nav does not mention
 * (public viewers, agent rail sections) lands in a trailing "Elsewhere" group
 * rather than being dropped — a guide that silently omits a surface is the
 * failure this whole registry exists to prevent.
 */
export interface GuideGroup {
  label: string
  surfaces: SurfaceDescriptor[]
}

/** The route path a nav item points at, in routeTable's notation. */
function navItemPath(item: { path: string; tenant: boolean }): string {
  if (!item.tenant) return item.path
  return item.path ? `/w/:workspace/${item.path}` : '/w/:workspace'
}

/** The tenant index — matches only EXACTLY. See `navMatch`. */
const TENANT_INDEX = '/w/:workspace'

/**
 * Does `surfacePath` belong under the nav item at `navPath`?
 *
 * Exact match, or a detail route beneath it — `/w/:workspace/chat/:id` belongs
 * with `/w/:workspace/chat`, because a reader looking for "the chat page" wants
 * the list and the single chat together.
 *
 * The tenant index is the exception. `/w/:workspace` is a prefix of every tenant
 * route, so prefix-matching it would pull the whole app into the Projects entry.
 */
function navMatch(surfacePath: string, navPath: string): boolean {
  if (surfacePath === navPath) return true
  if (navPath === TENANT_INDEX) return false
  return surfacePath.startsWith(`${navPath}/`)
}

/**
 * Clusters for surfaces the nav does not name.
 *
 * Without these, 25 of the 41 descriptors land in one "Elsewhere" bucket — 61%
 * of the guide in a junk drawer, which defeats the point of grouping it by the
 * menu at all. (Measured before this existed.)
 *
 * These are assigned BEFORE the nav groups, which matters for exactly one case:
 * the ten rail sections all sit under `/w/:workspace/agents/:slug/`, so nav's
 * `/w/:workspace/agents` would otherwise absorb them into Fleet and leave this
 * group empty.
 */
const CLUSTERS: Array<{ label: string; match: (path: string) => boolean }> = [
  {
    // One agent's left rail — ten sections under a single agent.
    label: 'Agent workspace',
    match: (p) => p.startsWith('/w/:workspace/agents/:slug/'),
  },
  {
    // Pages someone reaches from a link you sent them, with no account.
    label: 'Shared links (no login)',
    match: (p) =>
      ['/share/:token', '/storyboard/:slug', '/narrative/:slug', '/walkthrough/:id',
       '/review/:id', '/invite/:token'].includes(p) || p.startsWith('/ddd-release/'),
  },
]

export function guideGroups(surfaces: SurfaceDescriptor[] = SURFACES): GuideGroup[] {
  const claimed = new Set<string>()
  const take = (pred: (p: string) => boolean) => {
    const members = surfaces.filter((s) => !claimed.has(s.path) && pred(s.path))
    members.forEach((m) => claimed.add(m.path))
    return members
  }

  // Assign clusters first (see CLUSTERS' note), but DISPLAY the nav groups
  // first, because that is the order a reader already knows from the header.
  const clusterGroups = CLUSTERS.map((c) => ({ label: c.label, surfaces: take(c.match) }))

  const navGroups = NAV_GROUPS.map((nav) => {
    const wanted = nav.items.map(navItemPath)
    return { label: nav.label, surfaces: take((p) => wanted.some((w) => navMatch(p, w))) }
  })

  const rest = surfaces.filter((s) => !claimed.has(s.path))

  return [
    ...navGroups.filter((g) => g.surfaces.length),
    ...clusterGroups.filter((g) => g.surfaces.length),
    ...(rest.length ? [{ label: 'Elsewhere', surfaces: rest }] : []),
  ]
}
