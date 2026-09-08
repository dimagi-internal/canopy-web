import { describe, expect, it } from 'vitest'
import { relativeAge } from './relativeAge'

const NOW = new Date('2026-09-08T12:00:00Z')
const ago = (secs: number) => new Date(NOW.getTime() - secs * 1000).toISOString()

const H = 3600
const D = 24 * H

describe('relativeAge', () => {
  it('keeps second granularity, because a heartbeat is meant to be seconds fresh', () => {
    // turnLog's relativeTime floors this whole range to "just now", which cannot
    // tell a runner that reported 2s ago from one that reported 59s ago.
    expect(relativeAge(ago(2), NOW)).toBe('2s ago')
    expect(relativeAge(ago(59), NOW)).toBe('59s ago')
  })

  it('climbs to minutes and hours', () => {
    expect(relativeAge(ago(90), NOW)).toBe('2m ago')
    expect(relativeAge(ago(2 * H), NOW)).toBe('2h ago')
    expect(relativeAge(ago(23 * H), NOW)).toBe('23h ago')
  })

  it('does not report a month and a half as 1087 hours', () => {
    // The regression this exists for: the Runners tab read "drilled 1087h ago".
    // 1087h is 45 days, which is far enough back that months is the unit a
    // reader wants — the point is only that the number is never 1087.
    expect(relativeAge(ago(1087 * H), NOW)).toBe('2mo ago')
    expect(relativeAge(ago(80 * H), NOW)).toBe('3d ago')
  })

  it('climbs past days into weeks, months and years', () => {
    expect(relativeAge(ago(10 * D), NOW)).toBe('1w ago')
    expect(relativeAge(ago(21 * D), NOW)).toBe('3w ago')
    expect(relativeAge(ago(60 * D), NOW)).toBe('2mo ago')
    expect(relativeAge(ago(400 * D), NOW)).toBe('1y ago')
  })

  it('never emits a unit a reader has to convert', () => {
    // Every step of the ladder, sampled: the number stays small enough to read.
    for (const secs of [0, 30, 61, 600, 5 * H, 30 * H, 6 * D, 20 * D, 90 * D, 800 * D]) {
      const out = relativeAge(ago(secs), NOW)
      const n = Number(out.match(/^\d+/)?.[0] ?? 0)
      expect(n, `${secs}s → ${out}`).toBeLessThan(60)
    }
  })

  it('reads a missing or unparseable timestamp as never', () => {
    expect(relativeAge(null, NOW)).toBe('never')
    expect(relativeAge('not a date', NOW)).toBe('never')
  })

  it('treats a slightly fast clock as fresh rather than negative', () => {
    expect(relativeAge(ago(-5), NOW)).toBe('0s ago')
    expect(relativeAge(ago(-3600), NOW)).toBe('in the future')
  })
})
