import { describe, expect, it } from 'vitest'
import { isScopedViewerPath, scopedPath } from './scopedLinks'

describe('scopedLinks', () => {
  it('scopes a path under its workspace, and leaves it flat without one', () => {
    expect(scopedPath('connect', '/review/abc')).toBe('/w/connect/review/abc')
    // No flat fallback: an artifact has one address, under its workspace.
    expect(() => scopedPath('', '/share/tok')).toThrow()
  })

  it('recognises exactly the three scoped viewers', () => {
    expect(isScopedViewerPath('/w/connect/walkthrough/abc')).toBe(true)
    expect(isScopedViewerPath('/w/connect/review/abc/')).toBe(true)
    expect(isScopedViewerPath('/w/connect/share/tok')).toBe(true)
    // The list page and every other tenant page stay behind the login.
    expect(isScopedViewerPath('/w/connect/walkthroughs')).toBe(false)
    expect(isScopedViewerPath('/w/connect/agents')).toBe(false)
    expect(isScopedViewerPath('/walkthrough/abc')).toBe(false)
  })
})
