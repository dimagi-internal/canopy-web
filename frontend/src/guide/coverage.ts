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
 * Routes that are not pages, each with the reason: the catch-all, and anything
 * whose only job is to send the browser somewhere else. Every route is either a
 * descriptor in surfaces.ts or an entry here — listed explicitly rather than
 * detected from the element, because an explicit list fails LOUDLY when a new
 * redirect is added (the coverage test names it) instead of silently excusing
 * it. A flat one's reason is also its tenancy reason (routeTenancy.test.ts).
 */
export const NOT_PAGES: Record<string, string> = {
  '/*': 'the not-found catch-all',
  '/': 'redirect: to the active workspace',
  '/app': 'redirect: the server-loadable door into the app, to the active workspace',
  '/timeline': 'redirect: legacy flat path → /w/:workspace/timeline',
  '/shareouts/*': 'redirect: legacy flat path → /w/:workspace/shareouts',
  '/walkthroughs': 'redirect: legacy flat path → /w/:workspace/walkthroughs',
  '/storyboards': 'redirect: legacy flat path → /w/:workspace/storyboards',
  '/huddles/*': 'redirect: legacy flat path → /w/:workspace/huddles',
  '/agents/*': 'redirect: legacy flat path → /w/:workspace/agents',
  '/ddd/*': 'redirect: legacy flat path → /w/:workspace/ddd',
  '/ddd-plans': 'redirect: retired, → /',
  '/me': 'redirect: your profile, now a section of Settings → /settings/profile',
  '/reviews': 'redirect: retired, → /',
  '/insights': 'redirect: retired feed, → /',
  '/projects': 'redirect: retired (pre-tenancy), → /',
  '/w/:workspace': "redirect: the workspace index, to the workspace's agents",
  '/w/:workspace/settings/runners': 'redirect: → settings/topology/runners',
  '/w/:workspace/settings/agent-access': 'redirect: → the agents table in settings/topology',
  '/w/:workspace/huddles/groups/:slug': "redirect: a work group's old page, to the project its check-in lives on",
  '/w/:workspace/chat': 'redirect: the old address of /w/:workspace/sessions',
  '/w/:workspace/chat/:id': 'redirect: the old address of /w/:workspace/sessions/:id',
  '/w/:workspace/projects/:agent/:ref': "redirect: a project has one page, in its agent's console",
  '/w/:workspace/projects/:agent/:ref/people/:person': "redirect: a contributor page lives in its agent's console",
  // The four pages that became sections of /w/:workspace/settings.
  '/w/:workspace/members': 'redirect: → settings/members',
  '/w/:workspace/connected-apps': 'redirect: → settings/connected-apps',
  '/w/:workspace/inbound': 'redirect: → settings/inbound',
  '/w/:workspace/slack': 'redirect: → settings/slack',
  // Short forms of settings sections.
  '/w/:workspace/topology': 'redirect: → settings/topology',
  '/w/:workspace/email': 'redirect: → settings/email',
  '/w/:workspace/access-requests': 'redirect: → settings/access-requests',
  '/w/:workspace/secrets': 'redirect: → settings/secrets',
  '/w/:workspace/retention': 'redirect: → settings/retention',
  '/w/:workspace/admission-rules': 'redirect: → settings/admission-rules',
  '/w/:workspace/agents/:slug': "the agent console's frame; its sections are the pages, and it opens on Projects",
  '/w/:workspace/agents/:slug/needs-you': 'redirect: → Tasks, filtered to Waiting on you',
  '/w/:workspace/agents/:slug/credentials': "redirect: → the agent's Settings",
  '/w/:workspace/agents/:slug/settings': "redirect: → its Overview tab, or the tab an old #anchor names",
  // Two nouns, Projects and Tasks: Work is Tasks, Inbox and Items are Tasks
  // filtered to Waiting on you, and Overview and Work products are Projects.
  '/w/:workspace/agents/:slug/work': 'redirect: → Tasks',
  '/w/:workspace/agents/:slug/inbox': 'redirect: → Tasks, filtered to Waiting on you',
  '/w/:workspace/agents/:slug/items': 'redirect: → Tasks, filtered to Waiting on you',
  '/w/:workspace/agents/:slug/overview': 'redirect: → Projects',
  '/w/:workspace/agents/:slug/work-products': 'redirect: → Projects',
  // Syncs are Status reports (a section of Turns); History is a view of Skills.
  '/w/:workspace/agents/:slug/syncs': 'redirect: → Turns',
  '/w/:workspace/agents/:slug/history': 'redirect: → Skills history',
}

export function isDocumentable(path: string): boolean {
  return !(path in NOT_PAGES)
}
