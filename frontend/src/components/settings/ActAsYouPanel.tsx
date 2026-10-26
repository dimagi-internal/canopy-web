import { useEffect, useState } from 'react'
import { listMyHostGrants, stopMyHostGrant, type HostGrant } from '@/api/hostGrants'

function time(iso: string): string {
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? '' : when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/**
 * Act-as-you permissions: each connected site whose panel you sent from, letting
 * an agent act as you (within your own access) on that page until it runs out.
 * Stop ends it at once; the agent cannot act as you there again until you send
 * from that page.
 */
export function ActAsYouPanel() {
  const [rows, setRows] = useState<HostGrant[] | null>(null)
  const [error, setError] = useState('')

  async function refresh() {
    const res = await listMyHostGrants()
    if (res) setRows(res)
    else setError('Could not load your act-as-you permissions.')
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function onStop(row: HostGrant) {
    if (await stopMyHostGrant(row.id)) await refresh()
    else setError('Could not stop that permission.')
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-act-as-you">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Act-as-you permissions</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          When you send a message in an agent panel on a connected site, that page lets the agent
          act as you there, within your own access, for 30 minutes at a time. Stop ends it now.
        </p>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {rows === null ? (
        !error && <p className="text-sm text-muted-foreground">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No agent can act as you anywhere right now.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Site</th>
              <th className="py-1 font-medium">Agent</th>
              <th className="py-1 font-medium">May read</th>
              <th className="py-1 font-medium">Until</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((g) => (
              <tr key={g.id} className="border-t border-border">
                <td className="py-1.5 text-foreground-secondary">{g.site}</td>
                <td className="py-1.5 text-muted-foreground">{g.agent_name || g.agent || 'any agent'}</td>
                <td className="py-1.5 text-muted-foreground font-mono">{g.scope || '—'}</td>
                <td className="py-1.5 text-muted-foreground">{time(g.expires_at)}</td>
                <td className="py-1.5 text-right">
                  <button type="button" onClick={() => void onStop(g)} className="text-destructive hover:underline">
                    Stop
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
