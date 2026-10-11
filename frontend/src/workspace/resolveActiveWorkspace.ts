// Pure workspace-selection logic, extracted so it can be unit-tested without a
// React renderer (jsdom + @testing-library/react ARE installed and used by
// other test files — this stays a plain function because it doesn't need
// one, not because a renderer is unavailable).

export interface WorkspaceLike {
  slug: string
}

/**
 * Pick the active workspace slug given the URL's :workspace segment (if any),
 * the workspace the caller last chose (remembered across root pages and
 * reloads), and the caller's memberships. Preference order:
 *   1. `urlSlug` when it names a workspace the user belongs to,
 *   2. `remembered` when it still names one,
 *   3. the first membership (stable default),
 *   4. null (no memberships yet).
 * An `urlSlug` that isn't a membership is ignored (the caller 404s / redirects).
 *
 * Without (2), a root page (/supervisor, /sessions) — whose URL carries no
 * workspace — snapped the header back to the first membership the moment you
 * left a tenant page, so picking Dimagi and then opening Sessions showed
 * marshellis again.
 */
export function resolveActiveWorkspace(
  workspaces: WorkspaceLike[],
  urlSlug: string | null,
  remembered: string | null = null,
): string | null {
  if (urlSlug && workspaces.some((w) => w.slug === urlSlug)) return urlSlug
  if (remembered && workspaces.some((w) => w.slug === remembered)) return remembered
  return workspaces[0]?.slug ?? null
}
