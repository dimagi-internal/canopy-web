import type { RouteObject } from 'react-router-dom'

/**
 * Flatten the route table into absolute path strings, joining child paths onto
 * their parent (the agent workspace's rail sections are children).
 */
export function flattenRoutePaths(routes: RouteObject[], parent = ''): string[] {
  const out: string[] = []
  for (const r of routes) {
    const raw = (r as { path?: string }).path
    const here =
      raw === undefined
        ? parent
        : raw.startsWith('/')
          ? raw
          : `${parent.replace(/\/$/, '')}/${raw}`
    if (raw !== undefined) out.push(here)
    const kids = (r as { children?: RouteObject[] }).children
    if (kids?.length) out.push(...flattenRoutePaths(kids, here))
  }
  return out
}

/**
 * Paths that need no descriptor: the catch-all, and anything whose only job is
 * to send the browser somewhere else. These are listed explicitly rather than
 * detected from the element, because an explicit list fails LOUDLY when a new
 * redirect is added (the coverage test names it) instead of silently excusing it.
 */
const NOT_DOCUMENTABLE = new Set([
  '*',
  '/w/:workspace/settings/runners',
  '/w/:workspace/settings/agent-access',
  '/',
  '/app', // the server-loadable door into the app; redirects to the default workspace
  '/timeline',
  '/shareouts/*',
  '/walkthroughs',
  '/storyboards',
  '/agents/*',
  '/ddd/*',
  '/ddd-plans',
  '/reviews',
  '/insights', // retired feed; redirects home
  // Old flat viewer links: they move to the same page under its workspace
  // (pages/FlatArtifactRedirect.tsx, canopy-web#1289).
  '/walkthrough/:id',
  '/review/:id',
  '/share/:token',
  // The retired workbench Projects page: the workspace index now redirects to
  // the workspace's agents, and old /projects links follow it.
  '/w/:workspace',
  '/w/:workspace/projects',
  '/projects',
  // The four pages that became sections of /w/:workspace/settings.
  '/w/:workspace/members',
  '/w/:workspace/connected-apps',
  '/w/:workspace/inbound',
  '/w/:workspace/slack',
  '/w/:workspace/agents/:slug/needs-you',
  '/w/:workspace/agents/:slug/credentials',
  // Two nouns, Projects and Tasks: Work is Tasks, Inbox and Items are Tasks
  // filtered to Waiting on you, and Overview and Work products are Projects.
  '/w/:workspace/agents/:slug/work',
  '/w/:workspace/agents/:slug/inbox',
  '/w/:workspace/agents/:slug/items',
  '/w/:workspace/agents/:slug/overview',
  '/w/:workspace/agents/:slug/work-products',
  // Syncs are Status reports (a section of Turns); History is a view of
  // Skills. Neither was a name that said what it held.
  '/w/:workspace/agents/:slug/syncs',
  '/w/:workspace/agents/:slug/history',
  '/w/:workspace/agents/:slug',
])

export function isDocumentable(path: string): boolean {
  if (NOT_DOCUMENTABLE.has(path)) return false
  if (path.endsWith('/*')) return false
  return true
}
