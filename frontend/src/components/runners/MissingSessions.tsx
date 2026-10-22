import { useState, type JSX } from 'react'

import {
  checkMissingSessions,
  listMissingSessions,
  sendUpMissingSessions,
  type MissingSessionsOut,
} from '@/api/harness'
import { relativeAge } from '@/lib/relativeAge'
import { usePolled } from '@/hooks/usePolled'

const compact = (n: number) => Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n)

/** A box's local sessions in repos canopy knows, that canopy has no record of —
 *  found by its usage report. Check asks the box for a full rescan; Send up files
 *  the chosen ones as sessions and asks the box for their transcripts. The
 *  runner's admins only (the server decides). */
export function MissingSessions({ runnerId }: { runnerId: string }): JSX.Element {
  const [data, setData] = useState<MissingSessionsOut | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  // While a rescan is pending, look again until the box has reported.
  usePolled(
    (signal) => listMissingSessions(runnerId)
      .then((d) => { if (!signal.aborted) setData(d) })
      .catch((e: unknown) => { if (!signal.aborted) setError(e instanceof Error ? e.message : String(e)) }),
    { ms: data?.usage_scan_requested_at ? 15_000 : null, deps: [runnerId] },
  )

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(''); setNote('')
    try { await fn() } catch (e: unknown) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const check = () => run(async () => setData(await checkMissingSessions(runnerId)))
  const sendUp = () => run(async () => {
    const ids = [...picked]
    const sent = await sendUpMissingSessions(runnerId, ids)
    setPicked(new Set())
    setNote(`Sent up ${sent.length} session${sent.length === 1 ? '' : 's'}; their history arrives as the box ships it.`)
    setData(await listMissingSessions(runnerId))
  })

  const now = new Date()
  const rows = data?.sessions ?? []
  const all = rows.length > 0 && picked.size === rows.length
  return (
    <div className="space-y-2 text-[12px]" data-testid="missing-sessions">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">
          {data?.usage_reported_at ? `Last usage report ${relativeAge(data.usage_reported_at, now, { seconds: false })}` : 'No usage report yet'}
          {data?.usage_scan_requested_at && ' · rescan asked, waiting for the box'}
        </span>
        <button type="button" onClick={() => void check()} disabled={busy || !!data?.usage_scan_requested_at}
                className="ml-auto min-h-8 rounded-md border border-border px-2.5 text-foreground disabled:opacity-50">
          Check for missing sessions
        </button>
        {rows.length > 0 && (
          <button type="button" onClick={() => void sendUp()} disabled={busy || picked.size === 0}
                  className="min-h-8 rounded-md border border-primary/40 px-2.5 text-primary disabled:opacity-50">
            Send up {picked.size || ''} selected
          </button>
        )}
      </div>
      {note && <p className="text-success">{note}</p>}
      {error && <p className="text-destructive">{error}</p>}
      {data && rows.length === 0 && <p className="text-muted-foreground">Canopy has every session this box reported.</p>}
      {rows.length > 0 && (
        <table className="w-full [&_td]:pr-3 [&_th]:pr-3">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-1 font-medium">
                <input type="checkbox" aria-label="Select all" checked={all}
                       onChange={() => setPicked(all ? new Set() : new Set(rows.map((r) => r.claude_session)))} />
              </th>
              <th className="font-medium">Repository</th><th className="font-medium">Session</th>
              <th className="font-medium">Last active</th><th className="text-right font-medium">From cache</th>
              <th className="text-right font-medium">Input · output</th><th className="font-medium">Files in</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((r) => (
              <tr key={r.claude_session}>
                <td className="py-1">
                  <input type="checkbox" aria-label={`Select ${r.title || r.claude_session}`}
                         checked={picked.has(r.claude_session)}
                         onChange={() => setPicked((p) => {
                           const n = new Set(p)
                           if (n.has(r.claude_session)) n.delete(r.claude_session); else n.add(r.claude_session)
                           return n
                         })} />
                </td>
                <td className="font-mono text-foreground">{r.repo}</td>
                <td className="max-w-[320px] truncate text-foreground-secondary" title={r.title}>{r.title || r.claude_session}</td>
                <td className="text-foreground-secondary">{r.last_at ? relativeAge(r.last_at, now, { seconds: false }) : '—'}</td>
                <td className="text-right tabular-nums">{compact(r.cache_read_tokens)}</td>
                <td className="text-right tabular-nums">{compact(r.input_tokens)} · {compact(r.output_tokens)}</td>
                <td className="font-mono text-foreground-secondary">{r.agent || r.files_in}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
