/**
 * The header's navigation model.
 *
 * Kept out of AppLayout.tsx so the grouping and the active-state rules are
 * testable without mounting the shell (which pulls in auth, workspaces,
 * presence sockets and the AI-status poll).
 *
 * The header answers the five questions a person arrives with, one entry each:
 *
 *   Home       — what needs me, and what changed (personal, every workspace)
 *   Agents ▾   — what the agents are doing, will do, and run on
 *   Work ▾     — the work: tasks, Triage, projects
 *   Demos ▾    — what the work produces (storyboards, walkthroughs, shareouts)
 *   Workspace ▾ — who is in it and how it is set up
 *
 * Chats sits beside Home as a plain link: it is where you talk to an agent, not
 * a set of pages to browse. Help (Guide, Get started, System) is not workspace
 * business and has its own `?` menu; personal pages are in the account menu.
 *
 * Links and menus are told apart by the chevron a menu carries. The menus are
 * what keeps the header inside a laptop's width: the flat row of 15 links it
 * replaced needed 1280px and still overflowed.
 */

import { SURFACES } from '@/guide/surfaces'
import { roleAllows, type Capability } from '@/lib/workspaceRoles'
import { scopedPath } from '../../lib/scopedLinks'

/** A single destination. `tenant` items resolve under /w/:workspace. */
export interface NavItem {
  /** Tenant items: the path segment under /w/:workspace ('' = the index).
   *  Global items: the absolute path. */
  path: string
  label: string
  tenant: boolean
  /** Shown only to someone whose role in the active workspace allows this — a
   *  link that always lands on "only an admin can see this" is a dead end. */
  capability?: Capability
}

/** One header entry: a plain link, or a menu of links. */
export type NavEntry =
  | { kind: 'link'; item: NavItem }
  | { kind: 'menu'; label: string; items: NavItem[] }

export const NAV: NavEntry[] = [
  // Personal, across every workspace you are in — see pages/home.
  { kind: 'link', item: { path: '/home', label: 'Home', tenant: false } },
  { kind: 'link', item: { path: 'chat', label: 'Chats', tenant: true } },
  {
    kind: 'menu',
    label: 'Agents',
    items: [
      { path: 'agents', label: 'All agents', tenant: true },
      { path: 'agents/new', label: 'New agent', tenant: true, capability: 'agent.work' },
      // The fleet syncing as a team: what each agent is on, and what they agree to push.
      { path: 'huddles', label: 'Huddles', tenant: true },
      // What will fire, and what did. Both scoped to this workspace, like every
      // other entry in the menu; the cross-workspace twins are reached from Home.
      { path: 'schedules', label: 'Schedules', tenant: true },
      { path: 'activity', label: 'Turn log', tenant: true },
      // What the agents run on. Admin pages (the map and its tables are `logs.read`).
      { path: 'settings/topology', label: 'Runners', tenant: true, capability: 'logs.read' },
    ],
  },
  {
    kind: 'menu',
    label: 'Work',
    items: [
      { path: 'tasks', label: 'Tasks', tenant: true },
      { path: 'triage', label: 'Triage', tenant: true },
      { path: 'projects', label: 'Projects', tenant: true },
      // Generated screens over the workspace's work, each drawn in a sandbox.
      { path: 'views', label: 'Views', tenant: true },
      // What projects serve to the whole workspace, to add by hand.
      { path: 'mcp', label: 'Shared MCP servers', tenant: true },
    ],
  },
  {
    kind: 'menu',
    label: 'Demos',
    items: [
      // What the work produces for people to read and review.
      { path: 'storyboards', label: 'Storyboards', tenant: true },
      { path: 'walkthroughs', label: 'Walkthroughs', tenant: true },
      { path: 'ddd', label: 'DDD runs', tenant: true },
      { path: 'shareouts', label: 'Shareouts', tenant: true },
    ],
  },
  {
    kind: 'menu',
    label: 'Workspace',
    items: [
      { path: 'settings/members', label: 'Members', tenant: true },
      // Is everything set up right and how is it used, tree-wide. Analytics and
      // Network are its tabs.
      { path: 'health', label: 'Health', tenant: true, capability: 'logs.read' },
      { path: 'timeline', label: 'Timeline', tenant: true },
      { path: 'settings', label: 'Settings', tenant: true },
    ],
  },
]

/** The `?` menu: how canopy works, the same in every workspace. */
export const HELP_ITEMS: NavItem[] = [
  { path: '/guide', label: 'Guide', tenant: false },
  { path: '/get-started', label: 'Get started', tenant: false },
  { path: '/system', label: 'System', tenant: false },
]

/** The account menu: pages about you, the same in every workspace. Profile is
 *  one entry: your memory (/people/me) and account settings (/settings) are its
 *  tabs. */
export const ACCOUNT_ITEMS: (NavItem & { desktopOnly?: boolean })[] = [
  { path: '/me', label: 'Profile', tenant: false },
  { path: '/sessions', label: 'Shared transcripts', tenant: false },
  { path: '/supervisor', label: 'Supervisor', tenant: false },
  { path: '/ide', label: 'IDE', tenant: false, desktopOnly: true },
]

/** A destination with its href already resolved against the active tenant. */
export interface ResolvedNavItem {
  href: string
  label: string
}

export type ResolvedNavEntry =
  | { kind: 'link'; item: ResolvedNavItem }
  | { kind: 'menu'; label: string; items: ResolvedNavItem[] }

function resolveItem(
  item: NavItem,
  opts: { active: string | null; role?: string | null },
): ResolvedNavItem[] {
  if (!item.tenant) return [{ href: item.path, label: item.label }]
  if (!opts.active) return []
  if (item.capability && !roleAllows(opts.role, item.capability)) return []
  return [{ href: scopedPath(opts.active, item.path ? `/${item.path}` : ''), label: item.label }]
}

/**
 * Resolve the header for the current viewer.
 *
 * Anonymous visitors get nothing: they only ever reach the app shell on a
 * public link route, and every nav destination is behind the login gate, so
 * the whole nav would be dead links that bounce to sign-in.
 *
 * Tenant items are omitted until the active workspace is known, which avoids
 * linking to a broken `/w//…` on first paint. A menu left with no items
 * renders no trigger at all rather than an empty menu.
 */
export function resolveNav(opts: {
  isAuthed: boolean
  active: string | null
  /** The viewer's role in `active`, for items that name a `capability`. */
  role?: string | null
}): ResolvedNavEntry[] {
  if (!opts.isAuthed) return []
  return NAV.flatMap((entry): ResolvedNavEntry[] => {
    if (entry.kind === 'link') {
      return resolveItem(entry.item, opts).map((item) => ({ kind: 'link' as const, item }))
    }
    const items = entry.items.flatMap((item) => resolveItem(item, opts))
    return items.length > 0 ? [{ kind: 'menu', label: entry.label, items }] : []
  })
}

/** Every resolved destination an entry stands for. */
export function entryItems(entry: ResolvedNavEntry): ResolvedNavItem[] {
  return entry.kind === 'link' ? [entry.item] : entry.items
}

/**
 * Is `pathname` at or below `href`?
 *
 * The workspace index (/w/<slug>) is a prefix of EVERY tenant route, so it has
 * to match exactly — otherwise an item linking there, and its whole menu,
 * would highlight on every tenant page.
 */
export function isNavItemActive(href: string, pathname: string): boolean {
  const isIndex = href === '/' || /^\/w\/[^/]+$/.test(href)
  return pathname === href || (!isIndex && pathname.startsWith(href + '/'))
}

/**
 * The one item the current route belongs to, across the whole header: the
 * LONGEST matching href. Settings and Runners (settings/topology) both prefix
 * /w/x/settings/topology/runners; only the more specific one is the page.
 */
export function activeHref(entries: ResolvedNavEntry[], pathname: string): string | null {
  let best: string | null = null
  for (const entry of entries) {
    for (const { href } of entryItems(entry)) {
      if (isNavItemActive(href, pathname) && (!best || href.length > best.length)) best = href
    }
  }
  return best
}

/** An entry is active when the current route's item is one of its own. */
export function isNavEntryActive(
  entry: ResolvedNavEntry,
  entries: ResolvedNavEntry[],
  pathname: string,
): boolean {
  const href = activeHref(entries, pathname)
  return href !== null && entryItems(entry).some((i) => i.href === href)
}

/**
 * Where the workspace switcher goes: the SAME page in the other workspace, so
 * switching from /w/dimagi/health lands on /w/marshellis/health rather than
 * on Agents. Only the section is kept (settings keeps its sub-section too): a
 * page about one thing — an agent, a chat, a huddle — names something that
 * belongs to the old workspace, so it falls back to its list. Anything outside
 * /w/ (a personal page) lands on the new workspace's Agents, as before.
 */
export function switchWorkspacePath(pathname: string, slug: string): string {
  const m = /^\/w\/[^/]+\/?(.*)$/.exec(pathname)
  if (!m || !m[1]) return scopedPath(slug, '/agents')
  const parts = m[1].split('/').filter(Boolean)
  if (parts[0] === 'settings') return scopedPath(slug, `/${parts.slice(0, 2).join('/')}`)
  return scopedPath(slug, `/${LIST_FOR[parts[0]] ?? parts[0]}`)
}

/** Sections that exist only as one item's page (a shared walkthrough, a thread),
 *  and the list a switch lands on instead of a 404: each such page's `home` in
 *  the registry. `nav.test.ts` checks every routed section resolves to a page. */
export const LIST_FOR: Record<string, string> = (() => {
  const section = (path: string) => path.replace('/w/:workspace/', '').split('/')[0]
  const pages = new Set(SURFACES.map((s) => s.path))
  const out: Record<string, string> = {}
  for (const s of SURFACES) {
    if (!s.home?.startsWith('/w/:workspace/') || !s.path.startsWith('/w/:workspace/')) continue
    const own = section(s.path)
    if (!pages.has(`/w/:workspace/${own}`)) out[own] = section(s.home)
  }
  return out
})()
