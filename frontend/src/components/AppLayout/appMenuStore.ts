/**
 * What the person's apps add to the account menu, kept for the whole page. It loads when
 * the app bar first draws and refreshes each time the menu opens, so opening the menu
 * shows the items at once instead of waiting on the server.
 */
import { useSyncExternalStore } from 'react'
import { getLocalAppMenu, type LocalAppMenu } from '@/api/localApps'

let current: LocalAppMenu | null = null
let inflight: Promise<void> | null = null
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Replace what the menu shows, from an answer that already carries the whole menu. */
export function setAppMenu(menu: LocalAppMenu | null): void {
  current = menu
  listeners.forEach((l) => l())
}

/** Load it again. Calls made while one is on its way share it. A failure keeps what was there. */
export function refreshAppMenu(): Promise<void> {
  inflight ??= Promise.resolve()
    .then(() => getLocalAppMenu())
    .then(setAppMenu, () => undefined)
    .finally(() => { inflight = null })
  return inflight
}

export function useAppMenu(): LocalAppMenu | null {
  return useSyncExternalStore(subscribe, () => current)
}

export function resetAppMenuForTests(): void {
  current = null
  inflight = null
}
