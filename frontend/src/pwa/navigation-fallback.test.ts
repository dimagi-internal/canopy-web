import { describe, expect, it } from 'vitest'
import { shouldServeShell } from './navigation-fallback'

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
