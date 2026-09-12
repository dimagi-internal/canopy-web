// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import clientSrc from '../api/client.v2.ts?raw'
import authSrc from './AuthProvider.tsx?raw'

/**
 * `isPublicLinkRoute` exists TWICE — in api/client.v2.ts (which decides whether
 * a 401 bounces the browser to Google) and in auth/AuthProvider.tsx (which
 * decides what to paint). A route added to one and not the other sends an
 * anonymous visitor holding a valid share link to a login screen.
 *
 * That is exactly what /storyboard/ did: the server served the page, the API
 * answered the share token fine, and an incidental /api/me 401 still bounced
 * the visitor to Google. Caught by opening the link, not by any test — hence
 * this one.
 */
// Harvests BOTH clause styles: `startsWith('/x/')` (a prefix) and
// `=== '/x'` (an exact match, used by `/about` so a future `/about-billing`
// or `/aboutus` route doesn't silently become public too — see C8). Missing
// either style here would make this test a silent no-op for that clause,
// exactly the failure mode this test exists to catch.
function publicPrefixes(source: string): string[] {
  const fn = source.slice(source.indexOf('function isPublicLinkRoute'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  const startsWithMatches = [...body.matchAll(/startsWith\(['"]([^'"]+)['"]\)/g)].map((m) => m[1])
  const exactMatches = [...body.matchAll(/===\s*['"]([^'"]+)['"]/g)].map((m) => m[1])
  return [...startsWithMatches, ...exactMatches].sort()
}

describe('isPublicLinkRoute', () => {
  it('lists the same routes in both copies', () => {
    expect(publicPrefixes(clientSrc)).toEqual(publicPrefixes(authSrc))
  })

  it('covers every chrome-less public surface', () => {
    const prefixes = publicPrefixes(clientSrc)
    for (const route of [
      '/review/', '/share/', '/ddd-release/', '/storyboard/', '/narrative/', '/invite/', '/about',
    ]) {
      expect(prefixes).toContain(route)
    }
  })

  it('/about is an exact match, not a prefix', () => {
    // Regression pin for C8: "/about" used to be startsWith, which would also
    // admit "/about-billing" or "/aboutus".
    expect(publicPrefixes(clientSrc)).not.toContain('/about/')
    expect(clientSrc).toMatch(/===\s*["']\/about["']/)
    expect(authSrc).toMatch(/===\s*["']\/about["']/)
  })
})
