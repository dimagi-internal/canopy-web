import { useSyncExternalStore } from 'react'

/**
 * Whether a page that asks for the room (today only /ide) has folded the app
 * bar away. The page owns the toggle; AppLayout only reads it, so every other
 * page keeps its header whatever this says.
 */

const KEY = 'canopy.appbar.hidden'
const listeners = new Set<() => void>()

function read(): boolean {
  try {
    return window.localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

let hidden = read()

export function setAppBarHidden(next: boolean) {
  hidden = next
  try {
    window.localStorage.setItem(KEY, next ? '1' : '0')
  } catch {
    // storage disabled — lasts for this tab only
  }
  for (const l of listeners) l()
}

function subscribe(l: () => void) {
  listeners.add(l)
  return () => listeners.delete(l)
}

export function useAppBarHidden(): boolean {
  return useSyncExternalStore(subscribe, () => hidden, () => false)
}
