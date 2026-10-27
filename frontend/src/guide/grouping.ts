import { ACCOUNT_ITEMS, HELP_ITEMS, NAV } from '@/components/AppLayout/nav'
import { SURFACES, type SurfaceDescriptor } from './surfaces'

/**
 * Group descriptors by the header's menus, so the guide's shape matches the
 * menu people actually look at. A page in no menu carries its own `group` in
 * the registry (the agent rail, pages shared by link). Anything still unclaimed
 * lands in a trailing "Elsewhere" group rather than being dropped — a guide that
 * silently omits a surface is the failure this whole registry exists to
 * prevent — and grouping.test.ts fails while it holds anything.
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

/**
 * Does `surface` belong under the nav item at `navPath`?
 *
 * Exact match, or a detail route beneath it — `/w/:workspace/chat/:id` belongs
 * with `/w/:workspace/chat`, because a reader looking for "the chat page" wants
 * the list and the single chat together. A page no menu links is matched by its
 * registry `home`, the page a reader reaches it from. (No nav item links the
 * bare tenant index any more — it redirects to Agents — so nothing here can
 * prefix-match the whole app.)
 */
function navMatch(surface: SurfaceDescriptor, navPath: string): boolean {
  const p = surface.home ?? surface.path
  return p === navPath || p.startsWith(`${navPath}/`)
}

export function guideGroups(surfaces: SurfaceDescriptor[] = SURFACES): GuideGroup[] {
  const claimed = new Set<string>()
  const take = (pred: (s: SurfaceDescriptor) => boolean) => {
    const members = surfaces.filter((s) => !claimed.has(s.path) && pred(s))
    members.forEach((m) => claimed.add(m.path))
    return members
  }

  // Claim a page's own `group` first — the agent rail sits under
  // /w/:workspace/agents/:slug/, which the Agents menu would otherwise absorb —
  // but DISPLAY the menus first, in the order a reader knows from the header.
  const labels = [...new Set(surfaces.flatMap((s) => (s.group ? [s.group] : [])))]
  const ownGroups = labels.map((label) => ({ label, surfaces: take((s) => s.group === label) }))

  // The header's entries, then the `?` menu, then the account menu. A plain
  // link (Home, Sessions) is a group of one, named for itself.
  const headerGroups = [
    ...NAV.map((e) => (e.kind === 'link' ? { label: e.item.label, items: [e.item] } : e)),
    { label: 'Help', items: HELP_ITEMS },
    { label: 'Your account', items: ACCOUNT_ITEMS },
  ]
  const navGroups = headerGroups.map((nav) => {
    const wanted = nav.items.map(navItemPath)
    return { label: nav.label, surfaces: take((s) => wanted.some((w) => navMatch(s, w))) }
  })

  const rest = surfaces.filter((s) => !claimed.has(s.path))

  return [
    ...navGroups.filter((g) => g.surfaces.length),
    ...ownGroups,
    ...(rest.length ? [{ label: 'Elsewhere', surfaces: rest }] : []),
  ]
}
