/**
 * A tab left open across a deploy asks for a bundle that no longer exists.
 *
 * The route sections here are `lazy()`, so their chunks are fetched the first
 * time you open that section — which can be hours after the page loaded. Asset
 * filenames are content-hashed, the container ships exactly one `dist/`, and a
 * deploy replaces it, so the moment a deploy lands every chunk THIS page has not
 * fetched yet is gone. `#708` made that miss an honest 404 instead of a white
 * page, and `RouteErrorBoundary` turns the 404 into "This section didn't load"
 * with a Try again button — a button whose only job is to do the manual refresh
 * we are trying to stop asking for. With six deploys in a day and a tab open all
 * day, that is the last reliable way to meet a hard refresh.
 *
 * A reload is the whole fix: the shell is fetched from the network now (see
 * vite.config.ts), so the reloaded page names the CURRENT chunks. So do it
 * automatically instead of drawing a button that says "click here to reload".
 *
 * Two guards, because a reload that can loop is worse than the bug:
 *
 *  - `navigator.onLine` must be true. Offline, the chunk is missing because
 *    there is no network, and reloading cannot conjure one.
 *  - at most one reload per RELOAD_COOLDOWN_MS. A genuine loop comes back within
 *    a second or two, so one bounded retry stops it; a real deploy an hour later
 *    is well outside the window and still heals itself.
 *
 * If either guard says no, the error is rethrown and the boundary renders as
 * before — the manual Try again is still there.
 */

const RELOAD_STAMP_KEY = 'canopy:stale-chunk-reload-at'
const RELOAD_COOLDOWN_MS = 10_000

/** Browsers word this differently; none of them give it a code. */
const CHUNK_ERROR_PATTERNS = [
  /failed to fetch dynamically imported module/i, // Chromium
  /error loading dynamically imported module/i, // Firefox
  /importing a module script failed/i, // Safari
  /dynamically imported module/i, // catch-all for rewordings
  /failed to load module script/i, // wrong MIME (a 404 answered as HTML)
]

export function isStaleChunkError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')
  return CHUNK_ERROR_PATTERNS.some((re) => re.test(message))
}

function reloadedRecently(now: number): boolean {
  try {
    const at = Number(sessionStorage.getItem(RELOAD_STAMP_KEY) ?? 0)
    return Number.isFinite(at) && now - at < RELOAD_COOLDOWN_MS
  } catch {
    // Private mode / storage disabled: no way to bound a loop, so don't start one.
    return true
  }
}

function stampReload(now: number): void {
  try {
    sessionStorage.setItem(RELOAD_STAMP_KEY, String(now))
  } catch {
    /* handled by reloadedRecently returning true */
  }
}

/**
 * The three things this module needs from the browser, in one place.
 *
 * A seam, not a test hack: `window.location.reload` cannot be stubbed in jsdom
 * (it is non-configurable), and there is no honest way to assert "the browser
 * navigated" without stubbing it. Naming the touchpoints also makes it obvious
 * that this file's only side effect is a reload.
 */
export const browser = {
  online: (): boolean => navigator.onLine,
  now: (): number => Date.now(),
  reload: (): void => window.location.reload(),
}

/** True if we reloaded. Exported for the test; callers use `lazyRoute`. */
export function recoverFromStaleChunk(
  error: unknown,
  deps: {
    online: boolean
    now: number
    reload: () => void
  },
): boolean {
  if (!isStaleChunkError(error)) return false
  if (!deps.online) return false
  if (reloadedRecently(deps.now)) return false
  stampReload(deps.now)
  deps.reload()
  return true
}

/**
 * Wrap a `lazy()` loader so a stale chunk reloads the page instead of surfacing
 * a broken section.
 *
 * On a successful load the stamp is cleared, so a LATER deploy in the same tab
 * gets its own reload rather than being suppressed by an unrelated one.
 */
export function lazyRoute<T>(load: () => Promise<T>): () => Promise<T> {
  return async () => {
    try {
      const mod = await load()
      try {
        sessionStorage.removeItem(RELOAD_STAMP_KEY)
      } catch {
        /* nothing to clear */
      }
      return mod
    } catch (error) {
      const reloading = recoverFromStaleChunk(error, {
        online: browser.online(),
        now: browser.now(),
        reload: browser.reload,
      })
      // The page is being replaced. Resolving or rejecting now would race the
      // navigation and flash the error boundary on the way out, so hang.
      if (reloading) return new Promise<T>(() => {})
      throw error
    }
  }
}
