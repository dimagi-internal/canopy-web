// A task list's keyboard: J/K move the focused row, Space or Enter opens it
// beside the list, X selects it (Shift+X extends from the last one), C files a
// new task, Esc clears the selection. Live only while focus is on the page
// itself — never while typing in a field, inside an open menu or dialog, or
// with Cmd/Ctrl/Alt held (those belong to the browser and the palette).
import { useEffect, useRef } from 'react'

export interface TaskKeys {
  /** The rows in the order they are shown. */
  order: readonly string[]
  focused: string | null
  setFocused: (key: string | null) => void
  onOpen: (key: string) => void
  onToggle: (key: string, shiftKey: boolean) => void
  onCreate?: () => void
  /** Esc with nothing open: clear the selection. */
  onEscape: () => void
  /** The peek closes itself on Esc; while it is open, Esc is its own. */
  peekOpen?: boolean
  enabled?: boolean
}

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
    return true
  }
  return Boolean(target.closest('[role="menu"], [role="dialog"], [role="listbox"]'))
}

/** Space and Enter on a focused button or link are that control's own. */
function pressable(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && Boolean(target.closest('button, a, [role="button"]'))
}

export function useTaskKeys(opts: TaskKeys): void {
  const latest = useRef(opts)
  latest.current = opts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const o = latest.current
      if (o.enabled === false || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key
      const { order, focused } = o
      const at = focused ? order.indexOf(focused) : -1
      const move = (to: number) => {
        const next = order[Math.max(0, Math.min(order.length - 1, to))]
        if (!next) return
        o.setFocused(next)
        document.querySelector(`[data-task="${next}"]`)?.scrollIntoView?.({ block: 'nearest' })
      }
      if (key === 'j') {
        e.preventDefault()
        move(at + 1)
      } else if (key === 'k') {
        e.preventDefault()
        move(at < 0 ? 0 : at - 1)
      } else if ((key === ' ' || key === 'Enter') && focused && at >= 0 && !pressable(e.target)) {
        e.preventDefault()
        o.onOpen(focused)
      } else if (key === 'x' && focused && at >= 0) {
        e.preventDefault()
        o.onToggle(focused, e.shiftKey)
      } else if (key === 'c' && o.onCreate && !e.shiftKey) {
        e.preventDefault()
        o.onCreate()
      } else if (key === 'Escape' && !o.peekOpen) {
        o.onEscape()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
