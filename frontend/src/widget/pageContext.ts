/**
 * What the agent is told about the page you are on.
 *
 * Two layers, because the useful cases need different depths:
 *
 *  1. **The route layer, always on.** Derived from the path alone, so every
 *     page gets it for free: which surface, which workspace, which object.
 *     That is already enough for "this whole feature set should go" — the
 *     thought you have while looking at a page and lose a second later.
 *  2. **The page layer, opt-in.** A component calls `usePageContext(() => …)`
 *     to contribute what is actually on screen — the items and their ages, the
 *     rows, the counts. Needed for "this inbox is stale", which is a claim
 *     about data the URL cannot express.
 *
 * Ordered rules, first match wins, mirroring `src/presence/routes.ts` — so a
 * specific pattern (an agent's Items tab) must precede the general one (the
 * agent workspace).
 *
 * Unlike presence, a page with no rule is NOT excluded: it still gets its path
 * and a generic label. Presence is opt-in because a badge on a page nobody can
 * collide on is noise; context is opt-OUT because an agent that does not know
 * what you are looking at is the problem this exists to solve.
 *
 * (`src/embed/` is the app that runs INSIDE the frame. This directory is the
 * host side — what canopy-web does to put the widget on its own pages.)
 */

export interface PageDescriptor {
  /** Human-readable surface name, as the agent should refer to it. */
  surface: string
  /** Stable identifiers the agent can look things up by. */
  params?: Record<string, string>
}

export interface PageContextRule {
  pattern: RegExp
  build: (m: RegExpMatchArray) => PageDescriptor
}

export const pageContextRules: PageContextRule[] = [
  // --- an agent's workspace: the rail section is the interesting part ------
  // Inbox and Items are where "this is stale" gets said, so they are named
  // specifically rather than collapsed into "the agent workspace".
  {
    pattern: /^\/w\/([^/]+)\/agents\/([^/]+)\/(items|inbox|turns|tasks|schedules|syncs|skills|runners|overview)/,
    build: (m) => ({
      surface: `the ${m[3]} view of agent ${m[2]}`,
      params: { workspace: m[1], agent: m[2], section: m[3] },
    }),
  },
  {
    pattern: /^\/w\/([^/]+)\/agents\/([^/]+)/,
    build: (m) => ({
      surface: `the agent workspace for ${m[2]}`,
      params: { workspace: m[1], agent: m[2] },
    }),
  },
  {
    pattern: /^\/w\/([^/]+)\/agents/,
    build: (m) => ({ surface: 'the agents list', params: { workspace: m[1] } }),
  },

  // --- DDD ----------------------------------------------------------------
  {
    pattern: /^\/w\/([^/]+)\/ddd\/([^/]+)\/([^/]+)/,
    build: (m) => ({
      surface: `DDD run ${m[3]} of narrative ${m[2]}`,
      params: { workspace: m[1], narrative: m[2], run: m[3] },
    }),
  },
  {
    pattern: /^\/w\/([^/]+)\/ddd\/([^/]+)/,
    build: (m) => ({
      surface: `the DDD narrative ${m[2]}`,
      params: { workspace: m[1], narrative: m[2] },
    }),
  },

  // --- other tenant surfaces ---------------------------------------------
  {
    pattern: /^\/w\/([^/]+)\/(timeline|activity|shareouts|walkthroughs|storyboards|members|schedules|chat)/,
    build: (m) => ({ surface: `the ${m[2]} page`, params: { workspace: m[1] } }),
  },
  {
    pattern: /^\/w\/([^/]+)\/?$/,
    build: (m) => ({ surface: 'the project workbench', params: { workspace: m[1] } }),
  },

  // --- personal / global --------------------------------------------------
  { pattern: /^\/supervisor/, build: () => ({ surface: 'the supervisor inbox' }) },
  { pattern: /^\/insights/, build: () => ({ surface: 'the cross-portfolio insights feed' }) },
  { pattern: /^\/activity/, build: () => ({ surface: 'the fleet activity log' }) },
  { pattern: /^\/schedules/, build: () => ({ surface: 'the personal schedule calendar' }) },
  { pattern: /^\/sessions/, build: () => ({ surface: 'my shared sessions' }) },
  { pattern: /^\/system/, build: () => ({ surface: 'the capability catalog' }) },
  { pattern: /^\/settings/, build: () => ({ surface: 'the settings page' }) },
]

/** The route layer for a path. Never null — an unmatched page still says where
 *  it is, because "I do not know what you are looking at" is the failure this
 *  exists to prevent. */
export function describePage(path: string): PageDescriptor {
  for (const rule of pageContextRules) {
    const match = path.match(rule.pattern)
    if (match) return rule.build(match)
  }
  return { surface: 'a canopy page' }
}

// --- the page layer --------------------------------------------------------
//
// A module-level registry rather than React context, for one reason: the
// consumer is not a React component. `provideContext` hands the widget a plain
// callback, and the widget lives outside the React tree (it mounts its own
// DOM). Reading a provider from inside the tree and mirroring it out would be
// a second copy of the same state.

type PageContributor = () => Record<string, unknown>

let contributor: PageContributor | null = null

/** Register what is on screen. Returns a disposer; the LAST registration wins,
 *  because a page has one current view — and a contributor left behind from a
 *  page you navigated away from would describe something no longer there. */
export function setPageContributor(fn: PageContributor): () => void {
  contributor = fn
  return () => {
    if (contributor === fn) contributor = null
  }
}

/** The query string as a plain object, or null when there is none.
 *
 *  Kept SEPARATE from `path` rather than appended to it, because the route
 *  rules are regexes anchored on a bare path — `/^\/w\/([^/]+)\/?$/` stops
 *  matching the moment a `?` arrives, so folding the search in would silently
 *  drop every page back to the generic descriptor.
 */
export function describeQuery(search: string): Record<string, string> | null {
  const trimmed = (search || '').replace(/^\?/, '')
  if (!trimmed) return null
  const out: Record<string, string> = {}
  for (const [k, v] of new URLSearchParams(trimmed)) out[k] = v
  return Object.keys(out).length ? out : null
}

/** Both layers, assembled at the moment a conversation opens.
 *
 *  `search` is the cheapest context there is. A page whose state lives in its
 *  URL — which is every page you can usefully link to — describes its own view
 *  for free, with no per-page code: `/insights?project=x&category=stale` says
 *  what is on screen as precisely as a hand-written contributor would, and
 *  cannot drift from it. It was being dropped, so a filtered page looked
 *  identical to an unfiltered one.
 */
export function buildPageContext(path: string, search = ''): Record<string, unknown> {
  const described = describePage(path)
  const query = describeQuery(search)
  const base: Record<string, unknown> = {
    surface: described.surface,
    path,
    ...(query ? { query } : {}),
    ...(described.params ? { params: described.params } : {}),
  }
  if (!contributor) return base
  try {
    return { ...base, onScreen: contributor() }
  } catch {
    // A throwing contributor must not cost the route layer too — knowing which
    // page you are on is the more important half.
    return base
  }
}
