import { describe, expect, it } from 'vitest'
import { placeBelow } from './anchoredPopover'

describe('placeBelow', () => {
  it('right-aligns to its anchor when that fits', () => {
    expect(placeBelow({ left: 600, right: 700, bottom: 30 }, 288, 1024)).toEqual({ top: 34, left: 412, width: 288 })
  })

  it('slides right rather than open off the left edge of a narrow panel', () => {
    // The chip sits left of New chat / expand / close in a 360px panel.
    const at = placeBelow({ left: 150, right: 220, bottom: 30 }, 288, 360)
    expect(at.left).toBe(8)
    expect(at.left + at.width).toBeLessThanOrEqual(352)
  })

  it('never wider than the viewport less its gutters', () => {
    const at = placeBelow({ left: 10, right: 60, bottom: 30 }, 288, 240)
    expect(at).toEqual({ top: 34, left: 8, width: 224 })
  })
})
