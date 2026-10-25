import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { switchToWaiting } from './switchToWaiting'

function container(controller: object | null) {
  const listeners: Record<string, () => void> = {}
  return {
    controller,
    addEventListener: vi.fn((type: string, fn: () => void) => {
      listeners[type] = fn
    }),
    fire: (type: string) => listeners[type]?.(),
  }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('switchToWaiting', () => {
  it('reloads once the new worker takes over', () => {
    // The banner's case: the poll found the deploy, so nothing in the plugin
    // was listening to reload the page.
    const c = container({})
    const skip = vi.fn()
    const reload = vi.fn()
    switchToWaiting(skip, { container: c as never, reload })
    expect(skip).toHaveBeenCalledOnce()
    expect(reload).not.toHaveBeenCalled()
    c.fire('controllerchange')
    expect(reload).toHaveBeenCalledOnce()
  })

  it('reloads anyway when the worker never answers', () => {
    const reload = vi.fn()
    switchToWaiting(vi.fn(), { container: container({}) as never, reload, timeoutMs: 3000 })
    vi.advanceTimersByTime(3000)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('reloads only once when both happen', () => {
    const c = container({})
    const reload = vi.fn()
    switchToWaiting(vi.fn(), { container: c as never, reload })
    c.fire('controllerchange')
    vi.advanceTimersByTime(10_000)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('reloads at once for a page no worker controls', () => {
    const reload = vi.fn()
    switchToWaiting(vi.fn(), { container: container(null) as never, reload })
    expect(reload).toHaveBeenCalledOnce()
  })
})
