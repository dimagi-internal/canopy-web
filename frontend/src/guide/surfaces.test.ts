import { describe, it, expect } from 'vitest'
import { SURFACES } from './surfaces'

/**
 * A descriptor says when to open a page and nothing the page or the server
 * already says: contents, counts, who may change what. Those drift; this keeps
 * them out. (Paths, `home`, `group` and `flat` are checked against the route
 * table by coverage.test.ts, grouping.test.ts and routeTenancy.test.ts.)
 */
const ROLE_GATE = /\b(admins?|owners?|editors?|maintainers?|superuser|viewers?|permissions?|passkey)\b/i

describe('descriptors hold only when to open the page', () => {
  it('has a one-sentence `use` for every page', () => {
    for (const s of SURFACES) {
      expect(s.use, `${s.path}: use is one line`).not.toMatch(/\n/)
      expect(s.use.length, `${s.path}: use is at most 110 characters`).toBeLessThanOrEqual(110)
      expect(s.use, `${s.path}: use is one sentence`).toMatch(/^[A-Z][^.]*\.$/)
    }
  })

  it('restates no role gate (the page and the server say who may do what)', () => {
    const gated = SURFACES.filter((s) => ROLE_GATE.test(s.use)).map((s) => `${s.path}: ${s.use}`)
    expect(gated).toEqual([])
  })

  it('restates no count or route (both drift)', () => {
    const bad = SURFACES.filter((s) => /\d|\/w\/|\/[a-z-]+\b/.test(s.use)).map((s) => `${s.path}: ${s.use}`)
    expect(bad).toEqual([])
  })

  it('carries only the fields the guide renders', () => {
    const allowed = new Set(['path', 'home', 'group', 'flat', 'title', 'use'])
    const extra = SURFACES.flatMap((s) => Object.keys(s).filter((k) => !allowed.has(k)).map((k) => `${s.path}.${k}`))
    expect(extra).toEqual([])
  })
})
