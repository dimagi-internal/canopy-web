import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'

export interface LoadOptions {
  /** Keep the last result on screen while a changed `deps` list loads the next one.
   *  Default `false`: a new `deps` list starts from `data: null`. A `reload()` always keeps it. */
  keepData?: boolean
}

export interface Load<T> {
  /** The last successful result, or `null` before the first (and, unless `keepData`, after `deps` change). */
  data: T | null
  /** The last load's failure, cleared by the next success or a `deps` change. */
  error: Error | null
  /** True while a load is in flight. */
  loading: boolean
  /** Load again with the same deps; the current `data` stays until the answer lands. */
  reload: () => void
  /** Patch `data` locally (an optimistic edit). A no-op while `data` is `null` is up to the updater. */
  setData: (next: T | null | ((prev: T | null) => T | null)) => void
}

/**
 * Load once, and again whenever `deps` change — the one-shot read behind a page or a panel.
 * (A read that repeats on a timer is `usePolled`.)
 *
 * - An answer that lands after a newer load started, or after unmount, is dropped,
 *   and `fn` receives an AbortSignal that fires then (pass it to `fetch` if it can use it).
 * - `fn` may change identity every render; the latest one runs. `deps` names what it depends on.
 * - A failure is an `Error` in `error` (a non-Error throw is wrapped); `data` stays as it was.
 */
export function useLoad<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  opts: LoadOptions = {},
): Load<T> {
  const { keepData = false } = opts
  const [state, setState] = useState<{ data: T | null; error: Error | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  })
  const fnRef = useRef(fn)
  fnRef.current = fn
  const keepRef = useRef(keepData)
  keepRef.current = keepData
  const current = useRef<{ id: number; ctrl: AbortController } | null>(null)
  const nextId = useRef(0)

  const start = useCallback((fresh: boolean) => {
    current.current?.ctrl.abort()
    const mine = { id: ++nextId.current, ctrl: new AbortController() }
    current.current = mine
    setState((s) =>
      fresh && !keepRef.current ? { data: null, error: null, loading: true } : { ...s, loading: true },
    )
    fnRef.current(mine.ctrl.signal).then(
      (data) => {
        if (current.current?.id !== mine.id) return
        setState({ data, error: null, loading: false })
      },
      (e: unknown) => {
        if (current.current?.id !== mine.id) return
        const error = e instanceof Error ? e : new Error(String(e))
        setState((s) => ({ ...s, error, loading: false }))
      },
    )
  }, [])

  useEffect(() => {
    start(true)
    return () => {
      current.current?.ctrl.abort()
      current.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps is the caller's list
  }, deps)

  const reload = useCallback(() => start(false), [start])
  const setData = useCallback<Load<T>['setData']>(
    (next) =>
      setState((s) => ({ ...s, data: typeof next === 'function' ? (next as (p: T | null) => T | null)(s.data) : next })),
    [],
  )
  return { ...state, reload, setData }
}
