// Pure workspace-selection logic, extracted so it can be unit-tested without a
// React renderer (jsdom + @testing-library/react ARE installed and used by
// other test files — this stays a plain function because it doesn't need
// one, not because a renderer is unavailable).

export interface WorkspaceLike {
  slug: string
}

/**
 * Pick the active workspace slug given the URL's :workspace segment (if any)
 * and the caller's memberships. Preference order:
 *   1. `urlSlug` when it names a workspace the user belongs to,
 *   2. the first membership (stable default),
 *   3. null (no memberships yet).
 * An `urlSlug` that isn't a membership is ignored (the caller 404s / redirects).
 */
export function resolveActiveWorkspace(
  workspaces: WorkspaceLike[],
  urlSlug: string | null,
): string | null {
  if (urlSlug && workspaces.some((w) => w.slug === urlSlug)) return urlSlug
  return workspaces[0]?.slug ?? null
}
