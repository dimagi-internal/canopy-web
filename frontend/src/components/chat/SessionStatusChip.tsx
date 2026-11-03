import type { TurnView } from 'canopy-ui/chat'

/**
 * The chat header's one word on the turn — "needs you", "running", "queued" —
 * from the kit's `useTurnView`, the same answer the transcript's activity line
 * and Stop read, so the header and the panel never disagree. With nothing
 * live it names the session's runner ("idle · box"), or the one a new chat
 * will start on.
 */
export function SessionStatusChip({
  view,
  boundRunner,
  requestedRunner,
}: {
  view: TurnView
  /** The session's bound runner, for the idle line. */
  boundRunner?: string | null
  /** The runner a chat with no binding yet starts on. */
  requestedRunner?: string | null
}) {
  const on = view.runner ? ` · ${view.runner}` : ''
  if (view.phase === 'blocked') {
    return (
      <span data-testid="turn-phase" data-phase="blocked" className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-warning">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
        {view.label}{on}
      </span>
    )
  }
  if (view.phase === 'working') {
    return (
      <span data-testid="turn-phase" data-phase="working" className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-success">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
        {view.label}{on}
      </span>
    )
  }
  if (view.phase === 'queued') {
    return (
      <span data-testid="turn-phase" data-phase="queued" className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-muted-foreground">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-muted-foreground" />
        {view.label}{on}
      </span>
    )
  }
  if (boundRunner) {
    return (
      <span data-testid="turn-phase" data-phase="idle" className="shrink-0 text-[12px] text-muted-foreground">
        {view.label} · {boundRunner}
      </span>
    )
  }
  if (requestedRunner) {
    return (
      <span data-testid="turn-phase" data-phase="idle" className="shrink-0 text-[12px] text-muted-foreground">
        <span data-testid="chat-requested-runner">starts on {requestedRunner} with your first message</span>
      </span>
    )
  }
  return null
}
