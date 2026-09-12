/**
 * The header's navigation model.
 *
 * Kept out of AppLayout.tsx so the grouping and the active-state rules are
 * testable without mounting the shell (which pulls in auth, workspaces,
 * presence sockets and the AI-status poll).
 *
 * The nav is FOUR MENUS, not a flat row. It was a flat row of 15 links, which
 * outgrew the header: the inline nav only appeared at `xl` (1280px) and even
 * there it ran past the viewport, so it carried an `overflow-x-auto` and every
 * page got a horizontal scrollbar — while 1280px is Desktop Chrome's default
 * and most laptops sit at or below it. Grouping is what buys the room back;
 * the breakpoint then drops to `md` so the nav is actually visible on those
 * laptops instead of collapsing into the hamburger.
 *
 * Every item lives in a group — deliberately no bare top-level links mixed in
 * with menu triggers, so a reader never has to guess which labels navigate and
 * which open. `Projects` costing a click is covered by the `Canopy.` wordmark,
 * which already links to `/` and redirects to the active workspace's workbench.
 */

/** A single destination. `tenant` items resolve under /w/:workspace. */
export interface NavItem {
  /** Tenant items: the path segment under /w/:workspace ('' = the index).
   *  Global items: the absolute path. */
  path: string
  label: string
  tenant: boolean
}

export interface NavGroup {
  label: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    // What you are working on right now.
    label: 'Work',
    items: [
      { path: '', label: 'Projects', tenant: true },
      { path: 'chat', label: 'Chats', tenant: true },
      { path: '/insights', label: 'Insights', tenant: false },
    ],
  },
  {
    // The agents, and what they are doing / will do / have done.
    label: 'Fleet',
    items: [
      { path: '/supervisor', label: 'Supervisor', tenant: false },
      { path: 'agents', label: 'Agents', tenant: true },
      { path: '/activity', label: 'Activity', tenant: false },
      { path: '/schedules', label: 'Schedule', tenant: false },
    ],
  },
  {
    // The demo-driven-development artifacts.
    label: 'Demos',
    items: [
      { path: 'ddd', label: 'DDD', tenant: true },
      { path: 'walkthroughs', label: 'Walkthroughs', tenant: true },
      // The shared arcs — several narratives as one link. Without this entry a
      // board was reachable only from the link it was last pasted into.
      { path: 'storyboards', label: 'Storyboards', tenant: true },
    ],
  },
  {
    // Things published for, or about, the team — plus the workspace's own
    // admin. Shareouts and Sessions live here rather than under Demos: both
    // are work you publish for teammates to read, which is the workspace's
    // business, not a demo artifact.
    label: 'Workspace',
    items: [
      { path: 'shareouts', label: 'Shareouts', tenant: true },
      { path: 'timeline', label: 'Timeline', tenant: true },
      { path: '/sessions', label: 'Sessions', tenant: false },
      { path: 'members', label: 'Members', tenant: true },
      { path: 'inbound', label: 'Inbound', tenant: true },
      { path: '/system', label: 'System', tenant: false },
      { path: '/guide', label: 'Guide', tenant: false },
    ],
  },
]

/** A destination with its href already resolved against the active tenant. */
export interface ResolvedNavItem {
  href: string
  label: string
}

export interface ResolvedNavGroup {
  label: string
  items: ResolvedNavItem[]
}

/**
 * Resolve the groups for the current viewer.
 *
 * Anonymous visitors get nothing: they only ever reach the app shell on a
 * public link route, and every nav destination is behind the login gate, so
 * the whole nav would be dead links that bounce to sign-in.
 *
 * Tenant items are omitted until the active workspace is known, which avoids
 * linking to a broken `/w//…` on first paint. A group left with no items
 * renders no trigger at all rather than an empty menu.
 */
export function resolveNavGroups(opts: {
  isAuthed: boolean
  active: string | null
}): ResolvedNavGroup[] {
  if (!opts.isAuthed) return []
  return NAV_GROUPS.flatMap((group) => {
    const items = group.items.flatMap((item) => {
      if (!item.tenant) return [{ href: item.path, label: item.label }]
      if (!opts.active) return []
      return [{ href: `/w/${opts.active}${item.path ? `/${item.path}` : ''}`, label: item.label }]
    })
    return items.length > 0 ? [{ label: group.label, items }] : []
  })
}

/**
 * Is `pathname` at or below `href`?
 *
 * The workspace index (/w/<slug>) is a prefix of EVERY tenant route, so it has
 * to match exactly — otherwise Projects, and with it the whole Work menu,
 * highlights on every tenant page.
 */
export function isNavItemActive(href: string, pathname: string): boolean {
  const isIndex = href === '/' || /^\/w\/[^/]+$/.test(href)
  return pathname === href || (!isIndex && pathname.startsWith(href + '/'))
}

/** A group's trigger is active when the current route is one of its items. */
export function isNavGroupActive(group: ResolvedNavGroup, pathname: string): boolean {
  return group.items.some((item) => isNavItemActive(item.href, pathname))
}
