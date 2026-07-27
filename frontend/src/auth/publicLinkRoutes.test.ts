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
function publicPrefixes(source: string): string[] {
  const fn = source.slice(source.indexOf('function isPublicLinkRoute'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  return [...body.matchAll(/startsWith\(['"]([^'"]+)['"]\)/g)].map((m) => m[1]).sort()
}

describe('isPublicLinkRoute', () => {
  it('lists the same routes in both copies', () => {
    expect(publicPrefixes(clientSrc)).toEqual(publicPrefixes(authSrc))
  })

  it('covers every chrome-less public surface', () => {
    const prefixes = publicPrefixes(clientSrc)
    for (const route of [
      '/review/', '/share/', '/ddd-release/', '/storyboard/', '/narrative/', '/invite/',
    ]) {
      expect(prefixes).toContain(route)
    }
  })
})
