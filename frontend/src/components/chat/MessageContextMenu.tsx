import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RunnerOut } from '@/api/harness'

/**
 * The right-click (or long-press) menu on a chat message: Copy, and "Fork from
 * here", which starts a new session holding the conversation up to the end of
 * that message's turn (`POST /canopy-sessions/{id}/fork`); the page runs it and
 * opens the fork. The hover bar's Fork opens it too, to show progress or a refusal.
 */

/** Whether this session's runner can cut a fork. Mirrors the server's own
 *  refusal (`services.fork_runner_supported`), which stays the authority: a
 *  cloud runner, or a laptop on the Claude CLI runtime. Anywhere else the menu
 *  is not offered at all. */
export function canFork(runnerName: string | null | undefined, runners: readonly RunnerOut[]): boolean {
  const runner = runnerName ? runners.find((r) => r.name === runnerName) : undefined
  return Boolean(runner && (runner.kind === 'cloud' || runner.engine === 'claude-cli'))
}

/** The message a right-click landed on: every chat row carries its `turn_index`
 *  as `data-turn-index` (canopy-ui MessageList). */
export function turnIndexAt(target: EventTarget | null): number | null {
  if (!(target instanceof Element)) return null
  const raw = target.closest('[data-turn-index]')?.getAttribute('data-turn-index')
  const n = raw == null || raw === '' ? NaN : Number(raw)
  return Number.isInteger(n) && n >= 0 ? n : null
}

export function MessageContextMenu({
  x,
  y,
  busy,
  error,
  copyText = '',
  canFork: forkable = true,
  onFork,
  onDismiss,
}: {
  x: number
  y: number
  busy: boolean
  error: string | null
  /** What Copy writes; empty hides it. */
  copyText?: string
  /** Whether Fork from here is offered. */
  canFork?: boolean
  onFork: () => void
  onDismiss: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: x, top: y })

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const { width, height } = el.getBoundingClientRect()
    setPos({
      left: Math.max(4, Math.min(x, window.innerWidth - width - 4)),
      top: Math.max(4, Math.min(y, window.innerHeight - height - 4)),
    })
  }, [x, y, error])

  useEffect(() => {
    const away = (e: Event) => {
      if (ref.current && e.target instanceof Node && ref.current.contains(e.target)) return
      onDismiss()
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onDismiss()
    }
    // Armed on the next tick, as in the /ide row menu: the opening contextmenu
    // event is still bubbling and would otherwise close the menu it opened.
    const arm = window.setTimeout(() => {
      document.addEventListener('mousedown', away)
      document.addEventListener('contextmenu', away)
      document.addEventListener('keydown', key)
      window.addEventListener('blur', onDismiss)
      window.addEventListener('resize', onDismiss)
    }, 0)
    return () => {
      window.clearTimeout(arm)
      document.removeEventListener('mousedown', away)
      document.removeEventListener('contextmenu', away)
      document.removeEventListener('keydown', key)
      window.removeEventListener('blur', onDismiss)
      window.removeEventListener('resize', onDismiss)
    }
  }, [onDismiss])

  return (
    <div
      ref={ref}
      role="menu"
      data-testid="message-context-menu"
      style={{ left: pos.left, top: pos.top }}
      className="fixed z-50 w-64 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-lg"
      onContextMenu={(e) => e.preventDefault()}
    >
      {copyText && (
        <button
          type="button"
          role="menuitem"
          data-testid="message-menu-copy"
          onClick={() => {
            void navigator.clipboard?.writeText(copyText).catch(() => undefined)
            onDismiss()
          }}
          className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[13px] text-foreground hover:bg-muted"
        >
          Copy
        </button>
      )}
      {forkable && (
        <button
          type="button"
          role="menuitem"
          data-testid="message-menu-fork"
          disabled={busy}
          title="A new session with the conversation up to the end of this turn, listed under this one. It runs in the same worktree, so both sessions see and edit the same files."
          onClick={onFork}
          className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[13px] text-foreground enabled:hover:bg-muted disabled:cursor-default disabled:opacity-40"
        >
          {busy ? 'Forking…' : 'Fork from here'}
        </button>
      )}
      {error && (
        <div role="alert" className="px-2 pb-1 text-[11px] text-destructive">
          {error}
        </div>
      )}
    </div>
  )
}
