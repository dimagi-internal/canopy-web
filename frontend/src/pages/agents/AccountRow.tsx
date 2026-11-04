import type { ReactNode } from 'react'
import { StatusBadge } from 'canopy-ui/ui'

// One external account, one row: service name | status pill and one short detail |
// the action on the right. Anything longer (a failing check, a warning, a form that
// opens) goes underneath as small text, so every account on the page reads alike.

export type PillTone = 'success' | 'warning' | 'destructive' | 'neutral'

export function AccountRow({
  name,
  pill,
  detail,
  action,
  testId,
  id,
  children,
}: {
  name: string
  pill: { label: string; tone: PillTone } | null
  detail?: ReactNode
  action?: ReactNode
  testId?: string
  id?: string
  children?: ReactNode
}) {
  return (
    <div id={id} role="listitem" data-testid={testId} className="scroll-mt-6 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="w-36 shrink-0 text-[13px] text-foreground">{name}</div>
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2 text-[12px]">
          {pill ? <StatusBadge tone={pill.tone}>{pill.label}</StatusBadge> : <span className="text-muted-foreground">…</span>}
          {detail ? <span className="text-foreground-secondary">{detail}</span> : null}
        </div>
        {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
      </div>
      {children ? <div className="mt-1.5 space-y-1 text-[12px] text-muted-foreground sm:pl-[9.75rem]">{children}</div> : null}
    </div>
  )
}
