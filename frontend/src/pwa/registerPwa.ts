import { registerSW } from 'virtual:pwa-register'
import { markUpdateReady } from './updateReady'
import { swIsThisBuild, thisPageEntry } from './thisBuild'

// Adopt new service workers — but never underneath a page someone is looking at.
//
// The old arrangement was `registerType: 'autoUpdate'` plus a 60s poll calling
// registration.update(). autoUpdate forces skipWaiting + clientsClaim, so any of
// those ticks could activate a new SW MID-LOAD or mid-use. On activation
// cleanupOutdatedCaches() deletes the previous precache, and the live page — which
// was served the OLD index.html by navigateFallback — then asks for asset hashes
// that are gone from the cache and 404 on the server, because every deploy rehashes
// them. The modules fail and the app paints a WHITE PAGE. A manual reload fixes it,
// since by then the new SW serves the new shell.
//
// That is the reported bug: white on first load, fine after a refresh, and constant
// on a day with six deploys — the 60s poll turned a per-load race into a per-minute
// one.
//
// So: 'prompt' in vite.config.ts leaves the new SW WAITING, and we choose the
// moment. The polling below is kept — a long-lived client (installed PWA, menubar
// WKWebView, a tab open for days) otherwise never discovers a deploy at all, which
// is how the Sessions surface once got stuck on a pre-feature bundle.
//
// SINCE THEN: navigations are network-first, so the account above is history, not
// current behaviour — a page is no longer served a stale shell in the first place,
// and none of the rest can follow from it. What is still true is that a waiting SW
// holds a stale PRECACHE, which is what the app would fall back to offline. That is
// all adopting it promptly buys now, so waiting for `hidden` costs nothing.
const UPDATE_INTERVAL_MS = 60_000

/** Apply a waiting update only when the page is HIDDEN.
 *
 *  Reloading a visible page is its own bug here: this app's sign-in flow has a
 *  box you paste a single-use code into, and a surprise reload would throw it
 *  away. Hidden means the operator has looked elsewhere, so the reload costs
 *  them nothing and they come back to a current bundle. */
function applyWhenHidden(apply: () => void): void {
  if (document.visibilityState === 'hidden') {
    apply()
    return
  }
  const onHide = (): void => {
    if (document.visibilityState !== 'hidden') return
    document.removeEventListener('visibilitychange', onHide)
    apply()
  }
  document.addEventListener('visibilitychange', onHide)
}

export function registerPwa(): void {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator)) return

  /** The banner's switch. A page loaded past the SW (a hard reload) has no
   *  controller to change, so the plugin's reload-on-controlling never fires
   *  for it: reload it here, or the button does nothing. */
  const switchNow = (): void => {
    void updateSW(true)
    if (!navigator.serviceWorker.controller) window.location.reload()
  }

  let offered = false
  /** Offer the reload only when the server's build is not this page's (thisBuild.ts).
   *  `swSource` is the sw.js the poll just fetched; otherwise it is fetched here. */
  const offerIfNewer = async (swSource?: string): Promise<void> => {
    if (offered) return
    const registration = await navigator.serviceWorker.getRegistration()
    if (!registration?.waiting) return
    let source = swSource
    if (source === undefined) {
      try {
        const resp = await fetch(registration.waiting.scriptURL, { cache: 'no-store' })
        source = resp.ok ? await resp.text() : ''
      } catch {
        source = ''
      }
    }
    if (swIsThisBuild(source, thisPageEntry())) return
    offered = true
    markUpdateReady(switchNow)
    applyWhenHidden(() => void updateSW(true))
  }

  const updateSW = registerSW({
    immediate: true,
    // Fired once a new SW is installed and waiting. Nothing has been swapped
    // yet — the running page keeps the precache it was built against.
    // UpdateBanner offers the switch to a page that stays visible, but only when
    // the waiting build is not the page's own: one loaded after the deploy is
    // already current, and a hidden switch is all it needs.
    onNeedRefresh() {
      applyWhenHidden(() => void updateSW(true))
      void offerIfNewer()
    },
    onRegisteredSW(swUrl, registration) {
      if (!registration) return

      const check = async (): Promise<void> => {
        // Skip while an install is mid-flight or we're offline — retry next tick.
        if (registration.installing || !navigator.onLine) return
        try {
          // Bypass the HTTP cache so we actually see a freshly deployed sw.js;
          // a 200 means the server is reachable, so it's worth asking the browser
          // to re-evaluate the registration.
          const resp = await fetch(swUrl, {
            cache: 'no-store',
            headers: { 'cache-control': 'no-cache' },
          })
          if (resp.status !== 200) return
          const source = await resp.text()
          await registration.update()
          // workbox-window stops reporting updates after the first one it
          // judges external (any found by this poll more than 60s after load),
          // so a later deploy reaches onNeedRefresh only through this check.
          await offerIfNewer(source)
        } catch {
          // Offline / transient network error — the next tick will retry.
        }
      }

      setInterval(() => void check(), UPDATE_INTERVAL_MS)
      window.addEventListener('focus', () => void check())
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') void check()
      })
    },
  })
}
