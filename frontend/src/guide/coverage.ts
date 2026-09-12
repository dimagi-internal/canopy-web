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
  '/',
  '/timeline',
  '/shareouts/*',
  '/walkthroughs',
  '/storyboards',
  '/agents/*',
  '/ddd/*',
  '/ddd-plans',
  '/reviews',
  '/w/:workspace/agents/:slug/needs-you',
  '/w/:workspace/agents/:slug',
])

export function isDocumentable(path: string): boolean {
  if (NOT_DOCUMENTABLE.has(path)) return false
  if (path.endsWith('/*')) return false
  return true
}
