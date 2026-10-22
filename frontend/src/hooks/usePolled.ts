import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'

/** The period: a fixed number, `null` for "don't poll", or a function of the
 *  fetch that just settled (poll an open thread, stop once it closes; back off
 *  after an error). */
export type PollPeriod<T> = number | null | ((last: { data: T | undefined; error: Error | null }) => number | null)

export interface PolledOptions<T> {
  /** Poll period in ms, counted from the end of the previous fetch (a slow
   *  call never piles up behind itself). Changing it does not refetch: a new
   *  number takes over from the next tick (or starts the timer if none is
   *  running), `null` stops the timer. */
  ms: PollPeriod<T>
  /** Restart (fetch again, from scratch) when any of these change. */
  deps?: DependencyList
  /** `false` fetches nothing and polls nothing. Default `true`. */
  enabled?: boolean
  /** Fetch as soon as the hook starts (mount, `deps` change, `enabled` turning
   *  on). `false` waits one period first — for a poll layered over a fetch done
   *  elsewhere. Default `true`. */
  immediate?: boolean
  /** Also refresh when the window regains focus (switching desktop apps does
   *  not change `visibilityState`). Default `false`. */
  refreshOnFocus?: boolean
}

export interface Polled<T> {
  /** The last successful result; kept across a failed poll. */
  data: T | undefined
  /** The last fetch's failure, cleared by the next success. */
  error: Error | null
  /** True until the first fetch settles. */
  loading: boolean
  /** Fetch now and restart the period. Resolves when the fetch settles. */
  refresh: () => Promise<void>
  /** Patch `data` locally (an optimistic update the next poll will confirm). */
  setData: (next: T | undefined | ((prev: T | undefined) => T | undefined)) => void
}

const isHidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden'

/** Poll `fn` every `ms` while the page is visible.
 *
 *  While `document.visibilityState === 'hidden'` nothing is fetched: the timer
 *  stops, and coming back fetches at once and restarts it. /supervisor is a
 *  phone PWA, so a backgrounded tab must not keep the radio up. A result that
 *  lands after unmount or after `deps` change is dropped, and `fn` receives an
 *  AbortSignal that fires then. `fn` may change identity every render; the
 *  latest one is called. */
export function usePolled<T>(fn: (signal: AbortSignal) => Promise<T>, opts: PolledOptions<T>): Polled<T> {
  const { ms, deps = [], enabled = true, immediate = true, refreshOnFocus = false } = opts
  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(enabled && immediate)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const msRef = useRef(ms)
  msRef.current = ms
  // The running loop's controls; no-ops until the effect installs them.
  const loop = useRef<{ run: () => Promise<void>; reschedule: () => void }>({
    run: () => Promise.resolve(),
    reschedule: () => {},
  })

  useEffect(() => {
    if (!enabled) return
    const ctrl = new AbortController()
    let timer: ReturnType<typeof setTimeout> | null = null
    let inflight: Promise<void> | null = null
    let last: { data: T | undefined; error: Error | null } = { data: undefined, error: null }

    const stop = () => {
      if (timer) clearTimeout(timer)
      timer = null
    }
    const schedule = () => {
      stop()
      if (ctrl.signal.aborted || isHidden()) return
      const p = msRef.current
      const period = typeof p === 'function' ? p(last) : p
      if (period === null) return
      timer = setTimeout(() => void run(), period)
    }
    const run = (): Promise<void> => {
      stop()
      if (inflight) return inflight
      inflight = (async () => {
        try {
          const out = await fnRef.current(ctrl.signal)
          if (ctrl.signal.aborted) return
          last = { data: out, error: null }
          setData(out)
          setError(null)
        } catch (e) {
          if (ctrl.signal.aborted) return
          const err = e instanceof Error ? e : new Error(String(e))
          last = { data: last.data, error: err }
          setError(err)
        } finally {
          inflight = null
          if (!ctrl.signal.aborted) {
            setLoading(false)
            schedule()
          }
        }
      })()
      return inflight
    }
    // A changed period: start the timer if none runs, stop it on null. Never
    // fetches by itself.
    const reschedule = () => {
      if (inflight) return
      const p = msRef.current
      if (p === null) stop()
      else if (timer === null) schedule()
    }
    loop.current = { run, reschedule }

    const onVisibility = () => {
      if (isHidden()) stop()
      else void run()
    }
    const onFocus = () => void run()
    document.addEventListener('visibilitychange', onVisibility)
    if (refreshOnFocus) window.addEventListener('focus', onFocus)

    if (immediate && !isHidden()) void run()
    else schedule()

    return () => {
      ctrl.abort()
      stop()
      loop.current = { run: () => Promise.resolve(), reschedule: () => {} }
      document.removeEventListener('visibilitychange', onVisibility)
      if (refreshOnFocus) window.removeEventListener('focus', onFocus)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps is the caller's list
  }, [enabled, immediate, refreshOnFocus, ...deps])

  const fixedPeriod = typeof ms === 'function' ? 'fn' : ms
  useEffect(() => {
    loop.current.reschedule()
  }, [fixedPeriod])

  const refresh = useCallback(() => loop.current.run(), [])
  return { data, error, loading, refresh, setData }
}
