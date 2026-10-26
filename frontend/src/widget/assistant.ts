import { useSyncExternalStore } from 'react'

/** The part of canopy's own widget a page control may drive: open its panel
 *  from a button of the page's, where the floating bubble is hidden. */
export interface Assistant {
  toggle(): void
}

let current: Assistant | null = null
const listeners = new Set<() => void>()

/** Set by `CanopyWidget` once the widget is up, and cleared when it goes. */
export function setAssistant(next: Assistant | null): void {
  current = next
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The widget, or null while it is not mounted (or not enabled here). */
export function useAssistant(): Assistant | null {
  return useSyncExternalStore(subscribe, () => current, () => null)
}
