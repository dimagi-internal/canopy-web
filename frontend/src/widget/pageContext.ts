/**
 * What the agent is told about the page you are on.
 *
 * **The route layer**, derived from the path alone, so every page gets it for
 * free: which surface, which workspace, which object. That is already enough for
 * "this whole feature set should go" — the thought you have while looking at a
 * page and lose a second later.
 *
 * There used to be a second, opt-in layer here: `usePageContext` let a component
 * contribute what was on screen, and `buildPageContext` folded it in under
 * `onScreen`. It is gone, superseded by `pageState.ts`, which does the same job
 * without the flaw that made it unreliable — this one was READ ONCE, when a
 * conversation opened, so a user who filtered the page afterwards left the agent
 * describing a view that no longer existed. The state channel pushes instead.
 * `currentPageState(buildPageContext(...))` is how the two now compose: route
 * layer underneath, the page's own declaration on top.
 *
 * Ordered rules, first match wins, mirroring `src/presence/routes.ts` — so a
 * specific pattern (an agent's Items tab) must precede the general one (the
 * agent workspace).
 *
 * Unlike presence, a page with no rule is NOT excluded: it still gets its path
 * and a generic label. Presence is opt-in because a badge on a page nobody can
 * collide on is noise; the route layer is opt-OUT because an agent that does not
 * know what you are looking at is the problem this exists to solve.
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
    pattern: /^\/w\/([^/]+)\/agents\/([^/]+)\/(items|inbox|turns|tasks|schedules|syncs|skills|history|runners|overview)/,
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

/** The route layer for a path, assembled.
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
  return {
    surface: described.surface,
    path,
    ...(query ? { query } : {}),
    ...(described.params ? { params: described.params } : {}),
  }
}
