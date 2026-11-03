// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { useLoad } from './useLoad'

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('useLoad', () => {
  it('loads on mount', async () => {
    const { result } = renderHook(() => useLoad(() => Promise.resolve('a'), []))
    expect(result.current.loading).toBe(true)
    expect(result.current.data).toBeNull()
    await waitFor(() => expect(result.current.data).toBe('a'))
    expect(result.current.loading).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('reports a failure as an Error and keeps the data', async () => {
    let fail = false
    const { result } = renderHook(() => useLoad(() => (fail ? Promise.reject('boom') : Promise.resolve(1)), []))
    await waitFor(() => expect(result.current.data).toBe(1))
    fail = true
    act(() => result.current.reload())
    await waitFor(() => expect(result.current.error?.message).toBe('boom'))
    expect(result.current.data).toBe(1)
    expect(result.current.loading).toBe(false)
  })

  it('keeps the last data while the next deps load when asked to', async () => {
    const gate = deferred<number>()
    const { result, rerender } = renderHook(
      ({ k }) => useLoad(() => (k === 1 ? Promise.resolve(1) : gate.promise), [k], { keepData: true }),
      { initialProps: { k: 1 } },
    )
    await waitFor(() => expect(result.current.data).toBe(1))
    rerender({ k: 2 })
    expect(result.current.data).toBe(1)
    expect(result.current.loading).toBe(true)
    await act(async () => gate.resolve(2))
    expect(result.current.data).toBe(2)
  })

  it('clears the data on a deps change by default', async () => {
    const gate = deferred<number>()
    const { result, rerender } = renderHook(
      ({ k }) => useLoad(() => (k === 1 ? Promise.resolve(1) : gate.promise), [k]),
      { initialProps: { k: 1 } },
    )
    await waitFor(() => expect(result.current.data).toBe(1))
    rerender({ k: 2 })
    await waitFor(() => expect(result.current.data).toBeNull())
    await act(async () => gate.resolve(2))
    expect(result.current.data).toBe(2)
  })

  it('drops an answer that lands after a newer load, and aborts its signal', async () => {
    const first = deferred<string>()
    const second = deferred<string>()
    const signals: AbortSignal[] = []
    const queue = [first, second]
    const { result } = renderHook(() =>
      useLoad((signal) => {
        signals.push(signal)
        return queue.shift()!.promise
      }, []),
    )
    act(() => result.current.reload())
    expect(signals[0].aborted).toBe(true)
    await act(async () => second.resolve('new'))
    await act(async () => first.resolve('old'))
    expect(result.current.data).toBe('new')
  })

  it('drops an answer that lands after unmount', async () => {
    const gate = deferred<string>()
    const fn = vi.fn((signal: AbortSignal) => {
      void signal
      return gate.promise
    })
    const { unmount } = renderHook(() => useLoad(fn, []))
    const signal = fn.mock.calls[0][0]
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => gate.resolve('late'))
  })

  it('setData patches the result locally', async () => {
    const { result } = renderHook(() => useLoad(() => Promise.resolve([1, 2]), []))
    await waitFor(() => expect(result.current.data).toEqual([1, 2]))
    act(() => result.current.setData((cur) => (cur ? [...cur, 3] : cur)))
    expect(result.current.data).toEqual([1, 2, 3])
  })
})
