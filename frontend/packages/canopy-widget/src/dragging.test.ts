import { describe, expect, it } from 'vitest'

import {
  clampToViewport,
  isDrag,
  panelPosition,
  readSaved,
  storageKeyFor,
  writeSaved,
} from './dragging'

const BUBBLE = { width: 140, height: 48 }
const SCREEN = { width: 1000, height: 800 }

describe('keeping the bubble reachable', () => {
  it('leaves a position inside the viewport alone', () => {
    expect(clampToViewport({ x: 400, y: 300 }, BUBBLE, SCREEN)).toEqual({ x: 400, y: 300 })
  })

  it('pulls it back from past the right and bottom edges', () => {
    expect(clampToViewport({ x: 9999, y: 9999 }, BUBBLE, SCREEN)).toEqual({
      x: 1000 - 140 - 8,
      y: 800 - 48 - 8,
    })
  })

  it('pulls it back from negative coordinates', () => {
    expect(clampToViewport({ x: -500, y: -500 }, BUBBLE, SCREEN)).toEqual({ x: 8, y: 8 })
  })

  it('survives a viewport too small to hold it at all', () => {
    // A phone rotated, or a saved position read back on a smaller screen. The
    // bubble must end up at the near edge, never pushed off the far one by a
    // negative upper bound.
    const tiny = { width: 100, height: 40 }
    expect(clampToViewport({ x: 50, y: 50 }, BUBBLE, tiny)).toEqual({ x: 8, y: 8 })
  })
})

describe('telling a tap from a drag', () => {
  it('a still finger is a tap', () => {
    expect(isDrag({ x: 10, y: 10 }, { x: 10, y: 10 })).toBe(false)
  })

  it('a small wobble is still a tap — fingers are not precise', () => {
    expect(isDrag({ x: 10, y: 10 }, { x: 12, y: 12 })).toBe(false)
  })

  it('a deliberate move is a drag', () => {
    expect(isDrag({ x: 10, y: 10 }, { x: 40, y: 10 })).toBe(true)
  })

  it('measures distance, not either axis alone', () => {
    // 4px on each axis is 5.66px of travel. Checking axes separately would
    // call this a tap and leave the bubble stuck to diagonal drags.
    expect(isDrag({ x: 0, y: 0 }, { x: 4, y: 4 })).toBe(true)
  })
})

describe('where the panel opens once the bubble has moved', () => {
  const PANEL = { width: 380, height: 500 }

  it('sits above the bubble when there is room', () => {
    const at = panelPosition({ x: 600, y: 700, width: 140, height: 48 }, PANEL, SCREEN)
    expect(at.y).toBe(700 - 500 - 12)
  })

  it('flips below when the bubble is near the top', () => {
    // Dragging the bubble to the top must not open a panel off the top edge.
    const at = panelPosition({ x: 600, y: 20, width: 140, height: 48 }, PANEL, SCREEN)
    expect(at.y).toBe(20 + 48 + 12)
  })

  it('stays inside the viewport horizontally', () => {
    const at = panelPosition({ x: 10, y: 700, width: 140, height: 48 }, PANEL, SCREEN)
    expect(at.x).toBeGreaterThanOrEqual(8)
  })
})

describe('remembering where you put it', () => {
  function fakeStorage(initial: Record<string, string> = {}) {
    const data = { ...initial }
    return {
      getItem: (k: string) => data[k] ?? null,
      setItem: (k: string, v: string) => {
        data[k] = v
      },
      data,
    }
  }

  it('round-trips a position', () => {
    const s = fakeStorage()
    writeSaved(s, 'k', { x: 12, y: 34 })
    expect(readSaved(s, 'k')).toEqual({ x: 12, y: 34 })
  })

  it('ignores a value that is not a position', () => {
    expect(readSaved(fakeStorage({ k: '"hello"' }), 'k')).toBeNull()
    expect(readSaved(fakeStorage({ k: '{"x":1}' }), 'k')).toBeNull()
    expect(readSaved(fakeStorage({ k: 'not json' }), 'k')).toBeNull()
  })

  it('ignores NaN and Infinity, which JSON will happily carry back as null', () => {
    expect(readSaved(fakeStorage({ k: '{"x":null,"y":2}' }), 'k')).toBeNull()
  })

  it('survives storage that throws, which is a private window', () => {
    const hostile = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    }
    expect(readSaved(hostile, 'k')).toBeNull()
    expect(() => writeSaved(hostile, 'k', { x: 1, y: 2 })).not.toThrow()
  })

  it('keys per app, so two widgets on one origin do not fight', () => {
    expect(storageKeyFor('canopy-web')).not.toBe(storageKeyFor('connect-labs'))
  })
})
