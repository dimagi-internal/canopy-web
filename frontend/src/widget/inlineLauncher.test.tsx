// @vitest-environment jsdom
//
// A page that draws its own Canopy AI button says so while it is mounted, so the
// widget can hide its floating bubble on a phone, where it would cover the page.
import { describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import { hasInlineLauncher, onInlineLauncherChanged, useInlineLauncher } from './assistant'

describe('useInlineLauncher', () => {
  it('holds while a page is mounted, and is told each change', () => {
    const heard = vi.fn()
    const stop = onInlineLauncherChanged(heard)
    expect(hasInlineLauncher()).toBe(false)
    const a = renderHook(() => useInlineLauncher())
    const b = renderHook(() => useInlineLauncher())
    expect(hasInlineLauncher()).toBe(true)
    a.unmount()
    expect(hasInlineLauncher()).toBe(true)
    b.unmount()
    expect(hasInlineLauncher()).toBe(false)
    expect(heard).toHaveBeenCalledTimes(4)
    stop()
  })
})
