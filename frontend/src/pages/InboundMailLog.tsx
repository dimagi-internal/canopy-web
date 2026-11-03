import { useEffect, useState, type JSX } from 'react'

import { fetchInboundLog, type MailInboundLogOut } from '@/api/mail'
import { relativeAge } from '@/lib/relativeAge'

// What happened to each message that reached a domain: forwarded, handed to an agent, or
// dropped and why. Metadata only (no bodies), kept for `retention_days`. For "I sent mail
// and it never arrived": no row means it never reached canopy.

const OUTCOME: Record<string, string> = {
  forwarded: 'Forwarded',
  agent: 'Delivered to the agent',
  duplicate: 'Already handled',
  'dropped:spam': 'Dropped: SES marked it spam',
  'dropped:virus': 'Dropped: SES found a virus',
  'dropped:no-route': 'Dropped: no address or catch-all for it',
  'dropped:drop-route': 'Dropped: the address is set to drop',
  'dropped:unknown-domain': 'Dropped: not one of our domains',
  'dropped:mail-off': 'Dropped: mail is switched off',
  'dropped:not-a-member': 'Dropped: the destination left the workspace',
  'dropped:no-email': 'Dropped: the destination has no email',
  'dropped:loop': 'Dropped: would loop back into canopy',
  'stored:own-mail': 'Stored (the agent\'s own mail)',
  'stored:automated': 'Stored (automated mail, agent not woken)',
}

function describe(outcome: string): { text: string; bad: boolean } {
  const known = OUTCOME[outcome]
  if (known) return { text: known, bad: outcome.startsWith('dropped') }
  return { text: outcome, bad: outcome.startsWith('dropped') || outcome.startsWith('error') }
}

export function InboundMailLog({ workspace, domainId }: { workspace: string; domainId: number }): JSX.Element {
  const [rows, setRows] = useState<readonly MailInboundLogOut[] | null>(null)
  const [days, setDays] = useState(0)
  const [filter, setFilter] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const t = setTimeout(() => {
      fetchInboundLog(workspace, domainId, filter.trim())
        .then((r) => { if (alive) { setRows(r.items); setDays(r.retention_days); setError(null) } })
        .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : 'Failed to load the log'))
    }, filter ? 300 : 0)
    return () => { alive = false; clearTimeout(t) }
  }, [workspace, domainId, filter])

  return (
    <div data-testid="mail-inbound-log">
      <div className="flex flex-wrap items-center gap-3">
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Inbound mail</h4>
        <input aria-label="Filter by recipient" placeholder="Filter by recipient address" value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="min-h-11 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0" />
        {days > 0 && <span className="text-[11px] text-muted-foreground">{`Kept ${days} days, no message bodies.`}</span>}
      </div>
      {error ? (
        <p role="alert" className="mt-1 text-[12px] text-destructive">{error}</p>
      ) : rows === null ? (
        <p className="mt-1 text-[12px] text-muted-foreground">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="mt-1 text-[12px] text-muted-foreground">
          {filter ? 'Nothing for that address.' : 'No mail has reached this domain yet.'}
        </p>
      ) : (
        <table className="mt-1 w-full text-[12px]">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-1 pr-3 font-medium">When</th>
              <th className="py-1 pr-3 font-medium">To</th>
              <th className="py-1 pr-3 font-medium">From</th>
              <th className="py-1 pr-3 font-medium">Subject</th>
              <th className="py-1 font-medium">What happened</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const o = describe(r.outcome)
              return (
                <tr key={r.id} className="border-b border-border align-top">
                  <td className="whitespace-nowrap py-1.5 pr-3 text-muted-foreground">
                    <time dateTime={r.created_at} title={new Date(r.created_at).toLocaleString()}>{relativeAge(r.created_at)}</time>
                  </td>
                  <td className="py-1.5 pr-3 text-foreground">{r.recipient}</td>
                  <td className="py-1.5 pr-3 text-foreground-secondary">{r.original_from || '—'}</td>
                  <td className="max-w-[18rem] truncate py-1.5 pr-3 text-foreground-secondary" title={r.subject}>{r.subject || '(no subject)'}</td>
                  <td className={`py-1.5 ${o.bad ? 'text-destructive' : 'text-foreground'}`}>{o.text}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
