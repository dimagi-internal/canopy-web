import { describe, expect, it, vi } from 'vitest'

import { launcherVars, normalizeTheme, readableOn } from './theme'

/** A theme is written into style declarations on BOTH sides of the frame, so
 *  these are as much about what is refused as what is kept. */
describe('normalizeTheme', () => {
  it('keeps every field that is valid', () => {
    const t = normalizeTheme({
      mode: 'light',
      accent: '#2563eb',
      accentForeground: '#ffffff',
      radius: '8px',
      font: 'Inter, system-ui, sans-serif',
    })
    expect(t).toEqual({
      mode: 'light',
      accent: '#2563eb',
      accentForeground: '#ffffff',
      radius: '8px',
      font: 'Inter, system-ui, sans-serif',
    })
  })

  it('treats a bare number radius as px', () => {
    expect(normalizeTheme({ radius: 6 }).radius).toBe('6px')
  })

  it('returns nothing for no theme, so a host that sets none gets the widget it always got', () => {
    expect(normalizeTheme(undefined)).toEqual({})
  })

  it.each([
    ['a declaration break', 'red; background: url(https://evil.example/x)'],
    ['a url()', 'url(https://evil.example/x)'],
    ['a block', 'red } body { display: none'],
    ['a comment', 'red /* */'],
    ['an import', '@import "https://evil.example"'],
    ['markup', '</style><script>alert(1)</script>'],
  ])('drops an accent that is %s', (_why, value) => {
    const warn = vi.fn()
    expect(normalizeTheme({ accent: value }, warn).accent).toBeUndefined()
    // Silently doing nothing leaves a host no way to find out why its brand
    // colour never appeared.
    expect(warn).toHaveBeenCalledOnce()
  })

  it('drops a font that could escape its declaration', () => {
    const warn = vi.fn()
    expect(normalizeTheme({ font: 'Inter; color: red' }, warn).font).toBeUndefined()
    expect(warn).toHaveBeenCalled()
  })

  it('drops a negative or unit-less-string radius', () => {
    const warn = vi.fn()
    expect(normalizeTheme({ radius: -4 }, warn).radius).toBeUndefined()
    expect(normalizeTheme({ radius: 'big' }, warn).radius).toBeUndefined()
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('drops an unknown mode rather than guessing', () => {
    const warn = vi.fn()
    expect(normalizeTheme({ mode: 'sepia' as never }, warn).mode).toBeUndefined()
    expect(warn).toHaveBeenCalled()
  })

  it('derives readable text for the accent when the host gives none', () => {
    // Without this a dark brand colour on the default foreground is unreadable
    // in dark mode, where canopy's own --primary-foreground is near-black.
    expect(normalizeTheme({ accent: '#1e3a8a' }).accentForeground).toBe('#ffffff')
    expect(normalizeTheme({ accent: '#fde047' }).accentForeground).toBe('#000000')
  })

  it('never overrides a foreground the host chose', () => {
    expect(normalizeTheme({ accent: '#fde047', accentForeground: '#1c1917' }).accentForeground)
      .toBe('#1c1917')
  })
})

describe('readableOn', () => {
  it('reads hex in both lengths and rgb()', () => {
    expect(readableOn('#000')).toBe('#ffffff')
    expect(readableOn('#fff')).toBe('#000000')
    expect(readableOn('rgb(255, 255, 255)')).toBe('#000000')
  })

  it('falls back to white for a notation it cannot read without a browser', () => {
    expect(readableOn('oklch(0.6 0.2 40)')).toBe('#ffffff')
  })
})

describe('launcherVars', () => {
  it('maps only what was set, under the documented variable names', () => {
    expect(launcherVars({ accent: '#2563eb', radius: '8px' })).toEqual({
      '--canopy-accent': '#2563eb',
      '--canopy-radius': '8px',
    })
    expect(launcherVars({})).toEqual({})
  })
})
