/**
 * Which canopy page the widget is on, as the key canopy's server looks up when
 * it decides whether the agent may act as you there.
 *
 * canopy-web is a host of its own MCP (`apps/tokens/self_host.py`): on a
 * registered page, the widget's token mint also gets canopy a short, read-only
 * grant to call canopy's own tools AS you. The server owns the registry —
 * `PAGE_SCOPES` there maps each key to its scopes, and a key it does not know
 * gets no grant. This only NAMES the page; it cannot widen anything, because
 * the scopes come from the server and the tools run with your own access.
 *
 * Keep the keys in step with `PAGE_SCOPES` (a test on each side names them).
 * Every page here declares its selection with `usePageState` and a
 * `backingTool` among its scope's tools.
 */
const RULES: Array<[RegExp, string]> = [
  [/^\/w\/[^/]+\/agents\/[^/]+\/inbox\/?$/, 'agent.inbox'],
  [/^\/w\/[^/]+\/agents\/[^/]+\/skills\/history\/?$/, 'agent.skill_history'],
]

/** The grant page key for a router pathname, or '' for a page with none. */
export function grantPageFor(pathname: string): string {
  for (const [pattern, key] of RULES) {
    if (pattern.test(pathname)) return key
  }
  return ''
}

/** The widget's token URL for the page on screen now. A bare URL (no `page=`)
 *  on a page with no grant, so nothing changes there. */
export function tokenUrlFor(base: string, pathname: string): string {
  const page = grantPageFor(pathname)
  return page ? `${base}?page=${encodeURIComponent(page)}` : base
}
