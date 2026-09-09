import { describe, expect, it } from 'vitest'
import {
  navigationMatcher,
  navigationMatcherSource,
  shouldServeShell,
} from './navigation-fallback'

const UUID = '11111111-2222-3333-4444-555555555555'

describe('navigate-fallback ownership', () => {
  describe('SPA routes get the cached shell', () => {
    const spaPaths = [
      '/',
      '/supervisor',
      '/insights',
      '/system',
      '/settings',
      '/sessions',
      '/schedules',
      '/activity',
      '/timeline',
      '/shareouts/2026-07',
      '/walkthroughs',
      '/agents/echo',
      '/ddd-plans',
      '/reviews',
      '/review/abc',
      '/share/tok123',
      '/invite/tok123',
      '/ddd-release/nutrition-demo/run-1',
      '/w/connect',
      '/w/connect/ddd/nutrition-demo/nutrition-demo-2026-07-22-004',
      `/walkthrough/${UUID}`, // viewer shell (no /content)
    ]
    for (const p of spaPaths) {
      it(p, () => expect(shouldServeShell(p)).toBe(true))
    }
  })

  describe('server routes go to the network (never the shell)', () => {
    const serverPaths = [
      '/api/ddd/runs/x',
      '/accounts/google/login/',
      '/admin/',
      '/static/app.js',
      '/auth/cli/authorize/',
      '/health/',
      `/walkthrough/${UUID}/content`, // the reported bug: iframe stream
      `/walkthrough/${UUID}/content?t=tok`, // …even with a share token
      `/w/${UUID}/content`, // legacy redirect path
    ]
    for (const p of serverPaths) {
      it(p, () => expect(shouldServeShell(p)).toBe(false))
    }
  })

  it('an unknown path fails safe (network, not shell)', () => {
    expect(shouldServeShell('/foo/bar')).toBe(false)
    expect(shouldServeShell('/nope')).toBe(false)
  })

  describe('the /canopy labs mount behaves identically', () => {
    it('SPA route under /canopy → shell', () => {
      expect(shouldServeShell('/canopy/supervisor')).toBe(true)
      expect(shouldServeShell(`/canopy/walkthrough/${UUID}`)).toBe(true)
    })
    it('server stream under /canopy → network', () => {
      expect(shouldServeShell(`/canopy/walkthrough/${UUID}/content`)).toBe(false)
      expect(shouldServeShell('/canopy/api/me/')).toBe(false)
      expect(shouldServeShell('/canopy/accounts/google/login/')).toBe(false)
    })
    it('unknown under /canopy → network', () => {
      expect(shouldServeShell('/canopy/foo/bar')).toBe(false)
    })
  })
})

/**
 * The rule above is what `shouldServeShell` implements; what the browser
 * actually runs is the matcher whose source workbox-build inlines into sw.js.
 * They are generated from the same two arrays, and these tests are what stops
 * them drifting: every path asserted above is re-asserted through the real
 * matcher, so a change that only fixes one of the two fails here.
 */
describe('the matcher workbox inlines into the SW', () => {
  const matches = (path: string, mode = 'navigate'): boolean =>
    navigationMatcher()({
      request: { mode },
      url: new URL(path, 'https://labs.connect.dimagi.com'),
    })

  const everyPath = [
    '/',
    '/supervisor',
    '/insights',
    '/system',
    '/settings',
    '/sessions',
    '/schedules',
    '/activity',
    '/timeline',
    '/shareouts/2026-07',
    '/walkthroughs',
    '/agents/echo',
    '/ddd-plans',
    '/reviews',
    '/review/abc',
    '/share/tok123',
    '/invite/tok123',
    '/ddd-release/nutrition-demo/run-1',
    '/w/connect',
    '/w/connect/ddd/nutrition-demo/nutrition-demo-2026-07-22-004',
    `/walkthrough/${UUID}`,
    '/api/ddd/runs/x',
    '/accounts/google/login/',
    '/admin/',
    '/static/app.js',
    '/auth/cli/authorize/',
    '/health/',
    `/walkthrough/${UUID}/content`,
    `/walkthrough/${UUID}/content?t=tok`,
    `/w/${UUID}/content`,
    '/foo/bar',
    '/nope',
    '/canopy/supervisor',
    `/canopy/walkthrough/${UUID}`,
    `/canopy/walkthrough/${UUID}/content`,
    '/canopy/api/me/',
    '/canopy/accounts/google/login/',
    '/canopy/foo/bar',
  ]

  for (const path of everyPath) {
    it(`agrees with shouldServeShell on ${path}`, () => {
      expect(matches(path)).toBe(shouldServeShell(path))
    })
  }

  it('only claims navigations — a fetch/XHR for the same path is left alone', () => {
    // Everything the app fetches at runtime (the API above all) must bypass the
    // SW. The route is the only runtimeCaching entry, so `request.mode` is the
    // whole guard.
    expect(matches('/supervisor')).toBe(true)
    expect(matches('/supervisor', 'cors')).toBe(false)
    expect(matches('/supervisor', 'no-cors')).toBe(false)
    expect(matches('/api/me/', 'cors')).toBe(false)
  })

  it('is self-contained — it closes over nothing from this module', () => {
    // workbox-build serialises the function with String(fn) and drops the text
    // into sw.js, where NAVIGATE_FALLBACK_ALLOWLIST does not exist. A matcher
    // that referenced it would build fine and fail only in a real browser.
    const source = navigationMatcherSource()
    expect(source).not.toMatch(/NAVIGATE_FALLBACK_(ALLOW|DENY)LIST/)
    expect(source).toContain('request.mode')
  })
})
