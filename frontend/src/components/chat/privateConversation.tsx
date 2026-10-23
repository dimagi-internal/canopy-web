import { Lock } from 'lucide-react'
import { clsx } from 'clsx'

/**
 * How every list names a private (sensitive) conversation. It keeps no title on
 * purpose (a first sentence can name a person), so a list says what it is and
 * who started it, never an empty title or "(untitled)". The starter's name is not
 * content: the server sends it to anyone the session ACL lets list the row.
 */
export const PRIVATE_LABEL = 'Private conversation'

export interface PrivateFields {
  sensitive?: boolean
  started_by_name?: string
}

/** "Private conversation · Ann Smith", or just "Private conversation". */
export function privateTitle(s: PrivateFields): string {
  const who = (s.started_by_name ?? '').trim()
  return who ? `${PRIVATE_LABEL} · ${who}` : PRIVATE_LABEL
}

/** A list row's title: the private label for a sensitive conversation, else `fallback`. */
export function listTitle(s: PrivateFields, fallback: string): string {
  return s.sensitive ? privateTitle(s) : fallback
}

/** The lock a list draws before a private conversation's label. */
export function PrivateMark({ className }: { className?: string }) {
  return (
    <Lock
      className={clsx('inline h-3 w-3 shrink-0 align-[-1px] text-muted-foreground', className)}
      aria-label="Private"
      data-testid="private-mark"
    />
  )
}

/** A list row's title as rendered: the lock and the private label, or the title. */
export function ListTitle({ s, fallback, className }: { s: PrivateFields; fallback: string; className?: string }) {
  if (!s.sensitive) return <span className={className}>{fallback}</span>
  return (
    <span className={clsx('inline-flex min-w-0 items-center gap-1', className)} data-testid="private-title">
      <PrivateMark />
      <span className="truncate">{privateTitle(s)}</span>
    </span>
  )
}
