// @vitest-environment jsdom
import { describe, it, expect } from 'vitest'
import { routeTable } from '../router'
import { flattenRoutePaths, isDocumentable } from './coverage'
import { SURFACES } from './surfaces'

const declared = flattenRoutePaths(routeTable).filter(isDocumentable)
const described = new Set(SURFACES.map((s) => s.path))

describe('guide coverage', () => {
  it('finds the real routes', () => {
    // A sanity floor: if the flattener silently returns nothing, both
    // directions below pass vacuously and the test proves nothing.
    expect(declared.length).toBeGreaterThan(20)
    expect(declared).toContain('/settings')
    expect(declared).toContain('/w/:workspace/agents')
  })

  it('documents every documentable route', () => {
    const missing = declared.filter((p) => !described.has(p))
    expect(missing, `routes with no descriptor in guide/surfaces.ts: ${missing.join(', ')}`).toEqual([])
  })

  it('has no descriptor for a route that does not exist', () => {
    // The direction that catches a descriptor orphaned by a deleted route —
    // which is how a generated guide starts lying.
    const declaredSet = new Set(declared)
    const orphans = [...described].filter((p) => !declaredSet.has(p))
    expect(orphans, `descriptors naming no real route: ${orphans.join(', ')}`).toEqual([])
  })

  it('gives every descriptor the fields the guide renders', () => {
    for (const s of SURFACES) {
      expect(s.title, `${s.path} has no title`).toBeTruthy()
      expect(s.what, `${s.path} has no 'what'`).toBeTruthy()
      expect(s.audience, `${s.path} has no audience`).toBeTruthy()
    }
  })
})

describe('isDocumentable', () => {
  it('excludes the catch-all and legacy aliases', () => {
    expect(isDocumentable('*')).toBe(false)
    expect(isDocumentable('/ddd-plans')).toBe(false)
    expect(isDocumentable('/w/:workspace/agents/:slug/needs-you')).toBe(false)
  })

  it('includes real surfaces', () => {
    expect(isDocumentable('/settings')).toBe(true)
    expect(isDocumentable('/w/:workspace/chat/:id')).toBe(true)
  })
})
