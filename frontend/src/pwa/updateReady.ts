import { useSyncExternalStore } from 'react'

/**
 * A newer build is installed and waiting, and how to switch to it.
 *
 * registerPwa applies a waiting update on its own only once the page is
 * hidden, so a window that stays on screen all day (the /ide workbench beside
 * an editor) never gets one and keeps running the old bundle. This store lets
 * UpdateBanner offer the same reload to someone looking at the page.
 */

let apply: (() => void) | null = null
const listeners = new Set<() => void>()

export function markUpdateReady(next: () => void): void {
  apply = next
  for (const l of listeners) l()
}

function subscribe(l: () => void) {
  listeners.add(l)
  return () => listeners.delete(l)
}

/** The reload that switches to the new build, or null while none is waiting. */
export function useUpdateReady(): (() => void) | null {
  return useSyncExternalStore(subscribe, () => apply, () => null)
}
