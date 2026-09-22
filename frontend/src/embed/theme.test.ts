// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'

import { applyFrameTheme } from './theme'

/**
 * The only way any host styling reaches the panel. It re-validates what the
 * host sent — the loader's own check ran in the HOST's page, where a host can
 * simply post a raw message that skipped it.
 */

const root = document.documentElement

afterEach(() => {
  root.removeAttribute('style')
  root.className = ''
  document.body.removeAttribute('style')
})

describe('applyFrameTheme', () => {
  it('recolours canopy through its own tokens, so every primary surface follows', () => {
    applyFrameTheme({ accent: '#2563eb' })
    expect(root.style.getPropertyValue('--primary')).toBe('#2563eb')
    expect(root.style.getPropertyValue('--ring')).toBe('#2563eb')
    // Derived, because canopy's dark --primary-foreground is near-black and a
    // dark brand colour would otherwise carry unreadable text.
    expect(root.style.getPropertyValue('--primary-foreground')).toBe('#ffffff')
  })

  it('sets the font on BODY, where it outranks the baked-in font-sans rule', () => {
    applyFrameTheme({ font: 'Inter, sans-serif' })
    expect(document.body.style.fontFamily).toContain('Inter')
    expect(root.style.fontFamily).toBe('')
  })

  it('switches light and dark on the document', () => {
    root.classList.add('dark')
    applyFrameTheme({ mode: 'light' })
    expect(root.classList.contains('dark')).toBe(false)
    applyFrameTheme({ mode: 'dark' })
    expect(root.classList.contains('dark')).toBe(true)
  })

  it('leaves the server-rendered mode alone when the theme names none', () => {
    root.classList.add('dark')
    applyFrameTheme({ accent: '#2563eb' })
    expect(root.classList.contains('dark')).toBe(true)
  })

  it('does not trust a host that skipped the loader and posted raw values', () => {
    applyFrameTheme({ accent: 'red; background: url(https://evil.example/x)', font: 'x; color: red' })
    expect(root.style.getPropertyValue('--primary')).toBe('')
    expect(document.body.style.fontFamily).toBe('')
  })

  it('treats garbage as no theme at all', () => {
    for (const junk of [null, 'dark', 42, ['#000']]) applyFrameTheme(junk)
    expect(root.getAttribute('style') ?? '').toBe('')
  })

  it('replaces rather than merges, so a dropped accent does not stick', () => {
    applyFrameTheme({ accent: '#2563eb', radius: 4 })
    applyFrameTheme({ mode: 'light' })
    expect(root.style.getPropertyValue('--primary')).toBe('')
    expect(root.style.getPropertyValue('--radius')).toBe('')
  })
})
