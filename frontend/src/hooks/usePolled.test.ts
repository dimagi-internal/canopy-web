// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePolled } from './usePolled'

let visibility: DocumentVisibilityState = 'visible'

function setVisibility(v: DocumentVisibilityState) {
  visibility = v
  document.dispatchEvent(new Event('visibilitychange'))
}

// Let the awaited fn and the hook's finally run.
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

describe('usePolled', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    visibility = 'visible'
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('fetches on mount, exposes data, then polls every ms', async () => {
    let n = 0
    const fn = vi.fn(async () => ++n)
    const { result } = renderHook(() => usePolled(fn, { ms: 1000 }))
    expect(result.current.loading).toBe(true)
    await flush()
    expect(result.current.data).toBe(1)
    expect(result.current.loading).toBe(false)

    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(2)
    expect(result.current.data).toBe(2)
  })

  it('keeps the last data across a failure and clears the error on success', async () => {
    let fail = false
    const fn = vi.fn(async () => {
      if (fail) throw new Error('boom')
      return 'ok'
    })
    const { result } = renderHook(() => usePolled(fn, { ms: 1000 }))
    await flush()
    fail = true
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(result.current.data).toBe('ok')
    expect(result.current.error?.message).toBe('boom')
    fail = false
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(result.current.error).toBeNull()
  })

  it('stops while hidden and refetches at once when visible again', async () => {
    const fn = vi.fn(async () => 1)
    renderHook(() => usePolled(fn, { ms: 1000 }))
    await flush()
    expect(fn).toHaveBeenCalledTimes(1)

    setVisibility('hidden')
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(fn).toHaveBeenCalledTimes(1)

    act(() => setVisibility('visible'))
    await flush()
    expect(fn).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(3)
  })

  it('does not fetch at mount while hidden', async () => {
    visibility = 'hidden'
    const fn = vi.fn(async () => 1)
    renderHook(() => usePolled(fn, { ms: 1000 }))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(fn).not.toHaveBeenCalled()
    act(() => setVisibility('visible'))
    await flush()
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('drops a result that lands after unmount and aborts the signal', async () => {
    let resolve: (v: number) => void = () => {}
    let signal: AbortSignal | undefined
    const fn = vi.fn((s: AbortSignal) => {
      signal = s
      return new Promise<number>((r) => { resolve = r })
    })
    const { result, unmount } = renderHook(() => usePolled(fn, { ms: 1000 }))
    unmount()
    expect(signal?.aborted).toBe(true)
    await act(async () => { resolve(7) })
    expect(result.current.data).toBeUndefined()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('restarts when deps change, ignoring the stale in-flight result', async () => {
    const resolvers: Array<(v: string) => void> = []
    const fn = vi.fn(() => new Promise<string>((r) => resolvers.push(r)))
    const { result, rerender } = renderHook(({ id }) => usePolled(() => fn().then((v) => `${id}:${v}`), { ms: 1000, deps: [id] }), {
      initialProps: { id: 'a' },
    })
    rerender({ id: 'b' })
    expect(fn).toHaveBeenCalledTimes(2)
    await act(async () => { resolvers[0]('x') })
    expect(result.current.data).toBeUndefined()
    await act(async () => { resolvers[1]('y') })
    expect(result.current.data).toBe('b:y')
  })

  it('immediate: false waits one period; enabled: false does nothing', async () => {
    const fn = vi.fn(async () => 1)
    const { rerender } = renderHook(({ on }) => usePolled(fn, { ms: 1000, immediate: false, enabled: on }), {
      initialProps: { on: false },
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(fn).not.toHaveBeenCalled()
    rerender({ on: true })
    await flush()
    expect(fn).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('a period function decides from the settled fetch; null stops polling', async () => {
    let status = 'open'
    const fn = vi.fn(async () => status)
    renderHook(() => usePolled(fn, { ms: ({ data }) => (data === 'open' ? 1000 : null) }))
    await flush()
    status = 'closed'
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('a numeric period turning on starts the timer without an extra fetch', async () => {
    const fn = vi.fn(async () => 1)
    const { rerender } = renderHook(({ ms }) => usePolled(fn, { ms }), {
      initialProps: { ms: null as number | null },
    })
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(fn).toHaveBeenCalledTimes(1)
    rerender({ ms: 1000 })
    expect(fn).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('refresh fetches now and restarts the period; refreshOnFocus listens for focus', async () => {
    const fn = vi.fn(async () => 1)
    const { result } = renderHook(() => usePolled(fn, { ms: 1000, refreshOnFocus: true }))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    await act(async () => { await result.current.refresh() })
    expect(fn).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(fn).toHaveBeenCalledTimes(2)
    act(() => { window.dispatchEvent(new Event('focus')) })
    await flush()
    expect(fn).toHaveBeenCalledTimes(3)
  })
})
