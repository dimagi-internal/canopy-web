import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listMyHostGrants, stopMyHostGrant, type HostGrant } from '@/api/hostGrants'
import { listMyActAsRequests, stopActAsRequest, type ActAsRequest } from '@/api/actAs'

function time(iso: string): string {
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? '' : when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/**
 * Act-as-you permissions, in their two forms:
 * - on canopy itself: an agent, in a turn you started, that you let act as you
 *   (read-only or read + write) with a passkey touch, for 30 minutes while its turn runs;
 * - on a connected site: each site whose panel you sent from, letting an agent act as
 *   you (within your own access) on that page until it runs out.
 * Stop ends either at once.
 */
export function ActAsYouPanel() {
  const [rows, setRows] = useState<HostGrant[] | null>(null)
  const [canopy, setCanopy] = useState<ActAsRequest[]>([])
  const [error, setError] = useState('')

  async function refresh() {
    const res = await listMyHostGrants()
    if (res) setRows(res)
    else setError('Could not load your act-as-you permissions.')
    try {
      setCanopy(await listMyActAsRequests())
    } catch {
      setError('Could not load your act-as-you permissions.')
    }
  }

  async function onStopCanopy(row: ActAsRequest) {
    try {
      await stopActAsRequest(row.id)
      await refresh()
    } catch {
      setError('Could not stop that permission.')
    }
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
          An agent can act as you only after you confirm it with your passkey, for 30 minutes:
          on canopy, when an agent in a turn you started asks and you approve (read-only or
          read + write, only while that turn runs); on a connected site, when you send from its
          agent panel. Stop ends it now.
        </p>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div data-testid="act-as-canopy">
        <h3 className="text-xs font-semibold text-foreground">On canopy</h3>
        {canopy.length === 0 ? (
          <p className="mt-1 text-sm text-muted-foreground">No agent is asking or able to act as you on canopy.</p>
        ) : (
          <table className="mt-1 w-full text-xs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">Agent</th>
                <th className="py-1 font-medium">Why</th>
                <th className="py-1 font-medium">May</th>
                <th className="py-1 font-medium">Until</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {canopy.map((g) => (
                <tr key={g.id} className="border-t border-border">
                  <td className="py-1.5 text-foreground-secondary">{g.agent_name || g.agent}</td>
                  <td className="py-1.5 text-muted-foreground">{g.reason}</td>
                  <td className="py-1.5 text-muted-foreground">
                    {g.status === 'pending' ? (
                      <Link to={`/act-as/${g.id}`} className="text-primary hover:underline">
                        waiting for you
                      </Link>
                    ) : g.scope === 'write' ? (
                      'read + write'
                    ) : (
                      'read'
                    )}
                  </td>
                  <td className="py-1.5 text-muted-foreground">{time(g.expires_at ?? '')}</td>
                  <td className="py-1.5 text-right">
                    <button type="button" onClick={() => void onStopCanopy(g)} className="text-destructive hover:underline">
                      Stop
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <h3 className="text-xs font-semibold text-foreground">On connected sites</h3>
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
