import { useEffect, useSyncExternalStore } from 'react'

/** The part of canopy's own widget a page control may drive: open its panel
 *  from a button of the page's, where the floating bubble is hidden. */
export interface Assistant {
  toggle(): void
  /** Open the panel and ask this as the person's first message of a new conversation. */
  ask?(text: string): void
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

/** The panel now, outside React (a view's frame asking it a question). */
export function currentAssistant(): Assistant | null {
  return current
}

// ---- a page that carries its own "Canopy AI" button on a phone ----
// On a narrow screen the floating bubble sits over the bottom of the page — a form's
// Post button, a task row's status. A page that draws `CanopyAIButton` in its own
// header says so here, and while it is mounted the widget hides its bubble at phone
// width (`PHONE_QUERY`); wider, the bubble is back where it was.

/** Phone width: below Tailwind's `sm`. */
export const PHONE_QUERY = '(max-width: 639px)'

let inlineHosts = 0
const inlineListeners = new Set<() => void>()

function setInlineHosts(n: number): void {
  inlineHosts = n
  for (const listener of inlineListeners) listener()
}

/** Is a mounted page drawing its own Canopy AI button? Read by `CanopyWidget`. */
export function hasInlineLauncher(): boolean {
  return inlineHosts > 0
}

export function onInlineLauncherChanged(listener: () => void): () => void {
  inlineListeners.add(listener)
  return () => inlineListeners.delete(listener)
}

/** Call from a page that renders `CanopyAIButton` for phones (`sm:hidden`). */
export function useInlineLauncher(): void {
  useEffect(() => {
    setInlineHosts(inlineHosts + 1)
    return () => setInlineHosts(Math.max(0, inlineHosts - 1))
  }, [])
}
