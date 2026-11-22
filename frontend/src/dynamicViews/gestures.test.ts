import { describe, expect, it } from 'vitest'
import { GESTURE_WINDOW_MS, Gestures } from './gestures'

function setup() {
  const s = { clicked: false, now: 1_000 }
  return { s, g: new Gestures(() => s.clicked, () => s.now) }
}

describe('Gestures', () => {
  it('a gesture message with no live click is not believed', () => {
    const { g } = setup()
    g.note()
    expect(g.recent()).toBe(false)
    expect(g.active()).toBe(false)
  })

  it('a believed gesture outlives the click for the window, then ends', () => {
    const { s, g } = setup()
    s.clicked = true
    g.note()
    s.clicked = false
    s.now += GESTURE_WINDOW_MS - 1
    expect(g.recent()).toBe(true)
    s.now += 1
    expect(g.recent()).toBe(false)
  })

  it('spending it opens one lease, not two', () => {
    const { s, g } = setup()
    s.clicked = true
    g.note()
    s.clicked = false
    expect(g.recent(true)).toBe(true)
    expect(g.recent(true)).toBe(false)
  })

  it('a live click counts and is remembered', () => {
    const { s, g } = setup()
    s.clicked = true
    expect(g.active()).toBe(true)
    s.clicked = false
    expect(g.active()).toBe(true)
    g.forget()
    expect(g.active()).toBe(false)
  })
})
