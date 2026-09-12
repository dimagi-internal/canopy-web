import { describe, it, expect } from 'vitest'
import { USER_PATHS } from './paths'
import { describedPaths } from './surfaces'

describe('USER_PATHS', () => {
  it('describes the five ways in', () => {
    expect(USER_PATHS).toHaveLength(5)
  })

  it('has unique ids', () => {
    const ids = USER_PATHS.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('only references surfaces that are actually documented', () => {
    // The coupling that keeps the public page honest: it cannot promise a
    // surface the guide does not describe.
    const known = describedPaths()
    const dangling = USER_PATHS.flatMap((p) =>
      p.surfaces.filter((s) => s.startsWith('/') && !known.has(s)),
    )
    expect(dangling, `paths referencing undocumented surfaces: ${dangling.join(', ')}`).toEqual([])
  })

  it('tells every path where to start', () => {
    for (const p of USER_PATHS) {
      expect(p.startHere, `${p.id} has no starting point`).toBeTruthy()
      expect(p.who, `${p.id} does not say who it is for`).toBeTruthy()
    }
  })
})
