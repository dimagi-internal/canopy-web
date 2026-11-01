import { useEffect, useState } from 'react'
import type { AgentCredentialStatusOut } from '@/api/agents'
import {
  getCredentialReads,
  type AgentCredentialAlertOut,
  type AgentCredentialReadOut,
} from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'
import { AlertRows } from '@/pages/agents/AgentCredentialAlertsSection'

// Who has been handed one credential's value (Canopy Vault, canopy-web#1538), shown
// in the row's Activity panel — never the value. Grouped by person and machine, so
// 111 reads are the two or three places they came from; the raw list is one click
// further. Mounted only while the panel is open.

interface ReadGroup {
  who: string
  where: string
  count: number
  lastAt: string
}

function groupReads(reads: readonly AgentCredentialReadOut[]): ReadGroup[] {
  const groups = new Map<string, ReadGroup>()
  for (const r of reads) {
    const who = r.user_email || '—'
    const where = r.runner_name || '—'
    const key = `${who}|${where}`
    const g = groups.get(key)
    if (!g) groups.set(key, { who, where, count: 1, lastAt: r.created_at })
    else {
      g.count += 1
      if (r.created_at > g.lastAt) g.lastAt = r.created_at
    }
  }
  return [...groups.values()].sort((a, b) => b.lastAt.localeCompare(a.lastAt))
}

export function CredentialReadHistory({
  slug,
  row,
  alerts,
}: {
  slug: string
  row: AgentCredentialStatusOut
  /** The agent's read alerts (canopy-web#1549); those naming this ref show here too. */
  alerts?: AgentCredentialAlertOut[] | null
}) {
  const mine = (alerts ?? []).filter((a) => a.refs.includes(row.name))
  const [reads, setReads] = useState<AgentCredentialReadOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [raw, setRaw] = useState(false)

  useEffect(() => {
    let cancelled = false
    getCredentialReads(slug, row.name).then(
      (r) => !cancelled && setReads(r),
      (e) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        setReads([])
      },
    )
    return () => {
      cancelled = true
    }
  }, [slug, row.name])

  const groups = reads ? groupReads(reads) : []
  return (
    <div className="space-y-2 text-[11px]" data-testid={`cred-reads-list-${row.name}`}>
      <p className="text-muted-foreground">
        {row.last_read_at
          ? `Handed out ${row.read_count}× in all, last ${relativeAge(row.last_read_at)}.`
          : 'Never handed out.'}
      </p>
      {mine.length > 0 && (
        <div data-testid={`cred-alerts-${row.name}`}>
          <AlertRows alerts={mine} />
        </div>
      )}
      {reads === null ? (
        <p className="text-muted-foreground">…</p>
      ) : reads.length === 0 ? (
        <p className="text-muted-foreground">{error ?? 'No reads recorded.'}</p>
      ) : (
        <>
          <table className="w-full">
            <tbody className="divide-y divide-border">
              {groups.map((g) => (
                <tr key={`${g.who}|${g.where}`}>
                  <td className="py-1 text-foreground">{g.who}</td>
                  <td className="py-1 text-foreground">{g.where}</td>
                  <td className="py-1 text-muted-foreground">{g.count}×</td>
                  <td className="py-1 text-right text-muted-foreground">last {relativeAge(g.lastAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            type="button"
            onClick={() => setRaw(!raw)}
            className="text-muted-foreground underline-offset-2 hover:underline"
            aria-expanded={raw}
          >
            {raw ? 'Hide the individual reads' : `Show the ${reads.length} most recent reads`}
          </button>
          {raw && (
            <table className="w-full" data-testid={`cred-reads-raw-${row.name}`}>
              <tbody className="divide-y divide-border">
                {reads.map((r, i) => (
                  <tr key={`${r.created_at}-${i}`} title={r.turn_id || undefined}>
                    <td className="py-1 text-muted-foreground">{relativeAge(r.created_at)}</td>
                    <td className="py-1 text-foreground">{r.user_email || '—'}</td>
                    <td className="py-1 text-foreground">{r.runner_name || '—'}</td>
                    <td className="py-1 text-muted-foreground">{r.via}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}
