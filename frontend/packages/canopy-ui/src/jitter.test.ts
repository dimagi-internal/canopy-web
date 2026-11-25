import { describe, expect, it } from 'vitest'

import { jittered } from './jitter'

describe('jittered', () => {
  it('spreads a delay over ±50%', () => {
    expect(jittered(1_000, () => 0)).toBe(500)
    expect(jittered(1_000, () => 0.5)).toBe(1_000)
    expect(jittered(1_000, () => 0.999)).toBe(1_499)
  })

  it('stays inside the band with the real random source', () => {
    for (let i = 0; i < 200; i++) {
      const d = jittered(2_000)
      expect(d).toBeGreaterThanOrEqual(1_000)
      expect(d).toBeLessThanOrEqual(3_000)
    }
  })
})
