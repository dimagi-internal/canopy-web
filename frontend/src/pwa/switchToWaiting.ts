/**
 * Switch this page to the waiting build: tell the waiting service worker to
 * take over, then reload once it has.
 *
 * The reload is ours, not the plugin's. vite-plugin-pwa reloads on workbox's
 * `controlling` event, but it only listens for that after workbox itself
 * reported the update — and workbox stops reporting after the first one it
 * judges external, which is every update registerPwa's poll finds in a
 * long-open window. There the new worker took over and nothing reloaded, so
 * the banner's Reload did nothing.
 *
 * The timeout is the floor: navigations are network-first, so a plain reload
 * reaches the new build even if the worker never answers.
 */
export const SWITCH_TIMEOUT_MS = 3000

export interface SwitchDeps {
  container: Pick<ServiceWorkerContainer, 'controller' | 'addEventListener'> | undefined
  reload: () => void
  timeoutMs?: number
}

export function switchToWaiting(
  skipWaiting: () => void,
  {
    container = typeof navigator === 'undefined' ? undefined : navigator.serviceWorker,
    reload = () => window.location.reload(),
    timeoutMs = SWITCH_TIMEOUT_MS,
  }: Partial<SwitchDeps> = {},
): void {
  let done = false
  const reloadOnce = (): void => {
    if (done) return
    done = true
    reload()
  }
  // A page loaded past the SW (a hard reload) has no controller to change.
  if (!container?.controller) {
    skipWaiting()
    reloadOnce()
    return
  }
  container.addEventListener('controllerchange', reloadOnce, { once: true })
  skipWaiting()
  setTimeout(reloadOnce, timeoutMs)
}
