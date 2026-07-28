/** A resolved presence location: which roster to join, and where in it you are. */
export interface PageLocation {
  /** `<app>:<workspace|global>:<resource>` — the roster identity. */
  pageKey: string
  /** Human-readable position within the resource, for the expanded panel. */
  subLocation: string
}

export interface RouteRule {
  pattern: RegExp
  build: (m: RegExpMatchArray) => { workspace: string; resource: string; subLocation: string }
}

/**
 * Resolve a pathname to a presence location. Pure — the single place
 * grouping correctness lives.
 *
 * Rules are evaluated in order and the first match wins, so more specific
 * patterns must be listed before looser ones.
 *
 * Returns null when nothing matches. Callers render no badge in that case;
 * grouping every unrecognised route under one catch-all key would put
 * unrelated strangers in the same roster.
 */
export function pageKeyFor(
  app: string,
  pathname: string,
  rules: RouteRule[],
): PageLocation | null {
  for (const rule of rules) {
    const m = pathname.match(rule.pattern)
    if (!m) continue
    const { workspace, resource, subLocation } = rule.build(m)
    return { pageKey: `${app}:${workspace}:${resource}`, subLocation }
  }
  return null
}
