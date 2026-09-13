import { describe, it, expect } from 'vitest'
import { USER_ROLES } from './paths'
import { describedPaths } from './surfaces'

describe('USER_ROLES', () => {
  it('describes the four roles as a ladder', () => {
    expect(USER_ROLES).toHaveLength(4)
  })

  it('has unique ids', () => {
    const ids = USER_ROLES.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('only references surfaces that are actually documented', () => {
    // The coupling that keeps the public page honest: it cannot promise a
    // surface the guide does not describe.
    const known = describedPaths()
    const dangling = USER_ROLES.flatMap((p) => p.surfaces.filter((s) => !known.has(s)))
    expect(dangling, `paths referencing undocumented surfaces: ${dangling.join(', ')}`).toEqual([])
  })

  it('every surface reference is a route, not an external entry point', () => {
    // Filtering to `s.startsWith('/')` here would let a non-route reference
    // (a bare command name, a domain) slip past the check above entirely —
    // silently exempting it from "must be documented" rather than deciding
    // that on purpose. If a genuinely external entry point is ever needed,
    // that is a deliberate exception to carve out here, not a default.
    const nonRoutes = USER_ROLES.flatMap((p) => p.surfaces.filter((s) => !s.startsWith('/')))
    expect(nonRoutes, `non-route surface references: ${nonRoutes.join(', ')}`).toEqual([])
  })

  it('tells every path where to start', () => {
    for (const p of USER_ROLES) {
      expect(p.startHere, `${p.id} has no starting point`).toBeTruthy()
      expect(p.who, `${p.id} does not say who it is for`).toBeTruthy()
    }
  })

  it('says what enforces every tier', () => {
    // The field exists because these tiers are fully enforced on the agents
    // surface and only partly elsewhere. A role rendered without its
    // enforcement note is an aspirational claim.
    for (const r of USER_ROLES) {
      expect(r.enforcement, `${r.id} does not say what enforces it`).toBeTruthy()
    }
  })
})
