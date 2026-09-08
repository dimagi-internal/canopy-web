/**
 * WHEN a waiting service worker is allowed to take over.
 *
 * Extracted from `registerPwa` so the timing policy can be tested. It is the
 * risky half of the white-page fix: `registerType: 'prompt'` decides that the
 * new worker *waits*, and this decides when the wait ends. Left inline it was a
 * closure inside a registration callback, reachable only by mocking the
 * `virtual:pwa-register` module — so the one piece of logic with real edge cases
 * was the one piece with no tests.
 *
 * The rule: apply only while the page is HIDDEN. Applying means the page
 * reloads, and reloading a page someone is looking at is its own bug — the
 * sign-in flow has a box you paste a single-use code into, and a surprise reload
 * discards it.
 */

/** Reasons a page can go away, and why both are watched.
 *
 *  `visibilitychange` is the normal signal. `pagehide` is not redundant: an iOS
 *  tab backgrounding, or any page entering the back/forward cache, can deliver
 *  `pagehide` without a usable visibility transition. Whichever arrives first
 *  wins; the other is torn down. */
export interface HiddenGateDeps {
  /** Injected for tests. Defaults to the real document/window. */
  doc?: Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'>
  win?: Pick<Window, 'addEventListener' | 'removeEventListener'>
}

/**
 * Run `apply` the first time the page is hidden — or immediately, if it already is.
 *
 * One-shot per gate: `onNeedRefresh` fires once per newly installed worker, and
 * a day with six deploys fires it six times. Without a latch each call added its
 * own listener, they all survived to the same hide, and each asked for its own
 * reload.
 */
export function createHiddenGate(deps: HiddenGateDeps = {}) {
  const doc = deps.doc ?? (typeof document !== 'undefined' ? document : undefined)
  const win = deps.win ?? (typeof window !== 'undefined' ? window : undefined)
  let armed = false

  return function applyWhenHidden(apply: () => void): void {
    if (armed || !doc || !win) return
    armed = true

    const run = (): void => {
      doc.removeEventListener('visibilitychange', onHide)
      win.removeEventListener('pagehide', run)
      apply()
    }
    const onHide = (): void => {
      if (doc.visibilityState === 'hidden') run()
    }

    if (doc.visibilityState === 'hidden') {
      run()
      return
    }
    doc.addEventListener('visibilitychange', onHide)
    win.addEventListener('pagehide', run)
  }
}
