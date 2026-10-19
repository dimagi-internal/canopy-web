import type { JSX } from 'react'
import type { AgentMailboxOut } from '@/api/mail'

type Address = NonNullable<AgentMailboxOut['addresses']>[number]

/**
 * Every address an agent has (`GET /api/mail/agents/{slug}/mailbox` → `addresses`):
 * its own, its canopy-hosted routes and its Gmail mailboxes. An agent may hold several
 * — ada@dimagi-ai.com in Gmail beside ada@dimagi.ai here.
 */
export function AgentAddressList({ addresses, skipPrimary = false }: {
  addresses: readonly Address[] | undefined
  skipPrimary?: boolean
}): JSX.Element | null {
  const shown = (addresses ?? []).filter((a) => !(skipPrimary && a.primary))
  if (shown.length === 0) return null
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[12px]" data-testid="agent-addresses">
      {shown.map((a) => (
        <li key={a.address}>
          <span className="font-mono text-foreground">{a.address}</span>{' '}
          <span className="text-muted-foreground">
            ({a.backend === 'ses' ? 'canopy-hosted' : 'Gmail'}{a.primary ? ', primary' : ''})
          </span>
        </li>
      ))}
    </ul>
  )
}
