// `?` anywhere in the app (not while typing): every keyboard shortcut, from the
// one list in keys.ts that the screens themselves follow. Esc closes it (the shared
// Dialog owns Escape, the focus trap and the scrim; `?` toggles it from the window).
import { useEffect, useState, type JSX } from 'react'
import { Dialog, DialogContent, DialogTitle } from 'canopy-ui/ui'

import { SHORTCUTS, isPlainKey, isTypingTarget } from './keys'
import { Td, Tr } from '@/components/DataTable'

export function KeyboardHelp(): JSX.Element | null {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '?' || !isPlainKey(e) || isTypingTarget(e.target)) return
      // Another dialog (the palette) owns the keyboard while it is open.
      if (!open && document.querySelector('[aria-modal="true"]')) return
      e.preventDefault()
      setOpen((o) => !o)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        aria-modal="true"
        data-testid="keyboard-help"
        className="top-[10vh] block w-[calc(100%-2rem)] max-w-lg translate-y-0 bg-card p-4 shadow-xl"
      >
        <DialogTitle className="mb-2 text-sm leading-normal tracking-normal text-foreground">Keyboard shortcuts</DialogTitle>
        <p className="mb-3 text-[12px] text-muted-foreground">Single keys never fire while you are typing in a box.</p>
        <div className="grid gap-4 sm:grid-cols-2">
          {SHORTCUTS.map((g) => (
            <section key={g.title}>
              <h3 className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">{g.title}</h3>
              <table className="w-full text-[12.5px]">
                <tbody>
                  {g.shortcuts.map((s) => (
                    <Tr top key={s.keys.join('+') + s.what}>
                      <Td density="flush" nowrap>
                        {s.keys.map((k) => (
                          <kbd
                            key={k}
                            className="mr-0.5 rounded border border-border bg-muted px-1 font-mono text-[11px] text-foreground-secondary"
                          >
                            {k}
                          </kbd>
                        ))}
                      </Td>
                      <Td density="hair" tone="secondary">{s.what}</Td>
                    </Tr>
                  ))}
                </tbody>
              </table>
            </section>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
