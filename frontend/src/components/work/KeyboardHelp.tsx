// `?` anywhere in the app (not while typing): every keyboard shortcut, from the
// one list in keys.ts that the screens themselves follow. Esc closes it.
import { useEffect, useRef, useState, type JSX } from 'react'
import { X } from 'lucide-react'

import { SHORTCUTS, isPlainKey, isTypingTarget } from './keys'

export function KeyboardHelp(): JSX.Element | null {
  const [open, setOpen] = useState(false)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && open) {
        e.preventDefault()
        setOpen(false)
        return
      }
      if (e.key !== '?' || !isPlainKey(e) || isTypingTarget(e.target)) return
      // Another dialog (the palette) owns the keyboard while it is open.
      if (!open && document.querySelector('[aria-modal="true"]')) return
      e.preventDefault()
      setOpen((o) => !o)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  useEffect(() => {
    if (open) closeRef.current?.focus()
  }, [open])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-background/70 px-4 pt-[10vh]" onMouseDown={() => setOpen(false)}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="keyboard-help-title"
        data-testid="keyboard-help"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-lg rounded-lg border border-border bg-card p-4 shadow-xl"
      >
        <div className="mb-2 flex items-center justify-between">
          <h2 id="keyboard-help-title" className="text-sm font-semibold text-foreground">
            Keyboard shortcuts
          </h2>
          <button
            ref={closeRef}
            type="button"
            aria-label="Close"
            onClick={() => setOpen(false)}
            className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
        <p className="mb-3 text-[12px] text-muted-foreground">Single keys never fire while you are typing in a box.</p>
        <div className="grid gap-4 sm:grid-cols-2">
          {SHORTCUTS.map((g) => (
            <section key={g.title}>
              <h3 className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">{g.title}</h3>
              <table className="w-full text-[12.5px]">
                <tbody>
                  {g.shortcuts.map((s) => (
                    <tr key={s.keys.join('+') + s.what} className="align-top">
                      <td className="whitespace-nowrap py-0.5 pr-3">
                        {s.keys.map((k) => (
                          <kbd
                            key={k}
                            className="mr-0.5 rounded border border-border bg-muted px-1 font-mono text-[11px] text-foreground-secondary"
                          >
                            {k}
                          </kbd>
                        ))}
                      </td>
                      <td className="py-0.5 text-foreground-secondary">{s.what}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
