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
    const dangling = USER_PATHS.flatMap((p) => p.surfaces.filter((s) => !known.has(s)))
    expect(dangling, `paths referencing undocumented surfaces: ${dangling.join(', ')}`).toEqual([])
  })

  it('every surface reference is a route, not an external entry point', () => {
    // Filtering to `s.startsWith('/')` here would let a non-route reference
    // (a bare command name, a domain) slip past the check above entirely —
    // silently exempting it from "must be documented" rather than deciding
    // that on purpose. If a genuinely external entry point is ever needed,
    // that is a deliberate exception to carve out here, not a default.
    const nonRoutes = USER_PATHS.flatMap((p) => p.surfaces.filter((s) => !s.startsWith('/')))
    expect(nonRoutes, `non-route surface references: ${nonRoutes.join(', ')}`).toEqual([])
  })

  it('tells every path where to start', () => {
    for (const p of USER_PATHS) {
      expect(p.startHere, `${p.id} has no starting point`).toBeTruthy()
      expect(p.who, `${p.id} does not say who it is for`).toBeTruthy()
    }
  })
})
