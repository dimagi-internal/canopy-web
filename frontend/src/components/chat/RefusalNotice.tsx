import type { ReactNode } from 'react'
import { Ban } from 'lucide-react'

/**
 * A refusal as canopy says it everywhere (apps/agents/refusals.py): why, how to fix it, and who can.
 * The words are the server's; this only lays them out. Used above the chat composer when the
 * preview says you could not take a turn, and in the people panel when someone just added could not.
 */
export interface RefusalWords {
  code: string
  why: string
  how_to_fix?: string
  who_can_fix?: string
}

/** "you are not on…" → "You are not on…": the server starts `why` lower-case, to follow "not run: ". */
export function sentence(text: string): string {
  const t = text.trim()
  if (!t) return ''
  const s = `${t[0].toUpperCase()}${t.slice(1)}`
  return /[.!?]$/.test(s) ? s : `${s}.`
}

export function RefusalNotice({
  title,
  refusal,
  testId,
  children,
}: {
  title: string
  refusal: RefusalWords
  testId?: string
  /** An action that fixes it, when the viewer may take one. */
  children?: ReactNode
}) {
  return (
    <div
      role="status"
      data-testid={testId}
      data-code={refusal.code}
      className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-[13px] text-foreground"
    >
      <p className="m-0 flex items-start gap-1.5 font-medium">
        <Ban className="mt-[3px] h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
        <span>{title}</span>
      </p>
      <dl className="m-0 mt-1 max-w-3xl space-y-1 pl-5">
        <div>
          <dt className="sr-only">Why</dt>
          <dd className="m-0" data-testid={testId ? `${testId}-why` : undefined}>{sentence(refusal.why)}</dd>
        </div>
        {refusal.how_to_fix && (
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">How to fix it</dt>
            <dd className="m-0" data-testid={testId ? `${testId}-fix` : undefined}>{refusal.how_to_fix}</dd>
          </div>
        )}
        {refusal.who_can_fix && (
          <div>
            <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Who can fix it</dt>
            <dd className="m-0 break-words">{refusal.who_can_fix}</dd>
          </div>
        )}
      </dl>
      {children && <div className="mt-2 flex flex-wrap items-center gap-2 pl-5">{children}</div>}
    </div>
  )
}
