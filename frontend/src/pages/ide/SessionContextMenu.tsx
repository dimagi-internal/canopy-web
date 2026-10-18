import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { clsx } from 'clsx'
import type { ChatSession } from '@/api/chat'
import type { RunnerOut } from '@/api/harness'
import { closeIntent } from '@/components/chat/closeAction'
import { onlineSessionCapableRunners } from '@/components/chat/runnerEligibility'
import { deleteConsequence } from './ideTree'

/**
 * The /ide row's right-click menu. Every action is an existing session route
 * (close, transfer, stop, archive); this only offers them where you are looking.
 * The page runs the action and reports the outcome — the menu just picks one.
 */
export type IdeAction =
  | { kind: 'open-tab' }
  | { kind: 'copy-link' }
  | { kind: 'stop' }
  | { kind: 'transfer'; runner: RunnerOut }
  | { kind: 'archive' }
  | { kind: 'unarchive' }
  | { kind: 'close' }

function Item({
  onClick,
  disabled,
  title,
  danger,
  children,
  testId,
}: {
  onClick?: () => void
  disabled?: boolean
  title?: string
  danger?: boolean
  children: ReactNode
  testId?: string
}) {
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      title={title}
      onClick={onClick}
      data-testid={testId}
      className={clsx(
        'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[13px] disabled:cursor-default disabled:opacity-40',
        danger ? 'text-destructive enabled:hover:bg-destructive/10' : 'text-foreground enabled:hover:bg-muted',
      )}
    >
      {children}
    </button>
  )
}

const Sep = () => <div className="my-1 h-px bg-border" />

export function SessionContextMenu({
  session,
  x,
  y,
  runners,
  onAction,
  onDismiss,
}: {
  session: ChatSession
  x: number
  y: number
  runners: readonly RunnerOut[]
  onAction: (a: IdeAction) => void
  onDismiss: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: x, top: y })
  const [transferOpen, setTransferOpen] = useState(false)

  // Keep the menu on screen when opened near the bottom or right edge.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const { width, height } = el.getBoundingClientRect()
    setPos({
      left: Math.max(4, Math.min(x, window.innerWidth - width - 4)),
      top: Math.max(4, Math.min(y, window.innerHeight - height - 4)),
    })
  }, [x, y, transferOpen])

  useEffect(() => {
    const away = (e: Event) => {
      if (ref.current && e.target instanceof Node && ref.current.contains(e.target)) return
      onDismiss()
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onDismiss()
    }
    // Armed on the next tick: React flushes this effect while the opening
    // contextmenu event is still bubbling, so a listener added now would see
    // that same event reach `document` and close the menu it just opened.
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

  const archived = session.status !== 'active'
  const busy = Boolean(session.running || session.waiting_on_you)
  const close = closeIntent(session)
  const targets = onlineSessionCapableRunners(runners, session.runner_requirements).filter(
    (r) => r.name !== session.runner_name,
  )
  // Mirrors the server's own refusals (transfer_requests.request): nothing to
  // move off an unbound session, and an archived one cannot move.
  const transferBlocked = !session.runner_name
    ? 'This session has no runner to move off'
    : archived
      ? 'Archived sessions cannot move'
      : targets.length === 0
        ? 'No other online, session-capable runner is eligible'
        : null

  return (
    <div
      ref={ref}
      role="menu"
      data-testid="ide-context-menu"
      style={{ left: pos.left, top: pos.top }}
      className="fixed z-50 w-60 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-lg"
      onContextMenu={(e) => e.preventDefault()}
    >
      <div className="truncate px-2 pb-1 pt-0.5 text-[11px] text-muted-foreground" title={session.title}>
        {session.title?.trim() || '(untitled)'}
      </div>
      <Item onClick={() => onAction({ kind: 'open-tab' })}>Open in new tab</Item>
      <Item onClick={() => onAction({ kind: 'copy-link' })}>Copy link</Item>
      <Sep />
      <Item
        onClick={() => onAction({ kind: 'stop' })}
        disabled={!busy || archived}
        title={busy ? 'Cancel its open turn, or interrupt the agent' : 'Nothing is running'}
        testId="ide-menu-stop"
      >
        Stop
      </Item>
      <Item
        onClick={() => setTransferOpen((o) => !o)}
        disabled={Boolean(transferBlocked)}
        title={
          transferBlocked ??
          'Move this session, with its history, onto another runner. Onto a box you do not administer it becomes a request its admin approves.'
        }
        testId="ide-menu-transfer"
      >
        <span className="flex-1">Transfer to…</span>
        <span className="text-muted-foreground">{transferOpen ? '▾' : '▸'}</span>
      </Item>
      {transferOpen && !transferBlocked && (
        <div className="mb-1 ml-3 border-l border-border pl-1">
          {targets.map((r) => (
            <Item
              key={r.id}
              onClick={() => onAction({ kind: 'transfer', runner: r })}
              title={r.host}
              testId={`ide-menu-transfer-${r.id}`}
            >
              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-success" aria-hidden />
              <span className="truncate">{r.name}</span>
            </Item>
          ))}
        </div>
      )}
      <Sep />
      {archived ? (
        <Item onClick={() => onAction({ kind: 'unarchive' })} testId="ide-menu-unarchive">
          Unarchive
        </Item>
      ) : (
        <Item
          onClick={() => onAction({ kind: 'archive' })}
          title="Hide this row in canopy without touching emdash. Unarchive brings it back."
          testId="ide-menu-archive"
        >
          Archive
        </Item>
      )}
      <Item
        danger
        onClick={() => onAction({ kind: 'close' })}
        disabled={close.kind === 'blocked'}
        title={
          close.kind === 'blocked'
            ? close.why
            : deleteConsequence(session)
        }
        testId="ide-menu-close"
      >
        Delete session…
      </Item>
    </div>
  )
}
