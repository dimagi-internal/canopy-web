import { describe, it, expect } from 'vitest'
import { COMPONENTS, ONE_SENTENCE } from './components'

describe('COMPONENTS', () => {
  it('names five components with unique names', () => {
    expect(COMPONENTS).toHaveLength(5)
    expect(new Set(COMPONENTS.map((c) => c.name)).size).toBe(5)
  })

  it('explains each one', () => {
    for (const c of COMPONENTS) expect(c.what.length).toBeGreaterThan(20)
  })

  it('has a connecting sentence', () => {
    expect(ONE_SENTENCE).toContain('turn')
  })
})
