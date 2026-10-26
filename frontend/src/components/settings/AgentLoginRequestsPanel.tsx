import { useEffect, useState } from 'react'
import { decideMyAgentLoginRequest, listMyAgentLoginRequests, type AgentLoginRequestOut } from '@/api/agents'

// Requests, from an agent's admin, to make YOUR existing canopy account that agent's own
// login — after which the agent's token signs in as you. Only you can accept, here, with a
// passkey touch (the API client asks for it). Shown only when there is one.
export function AgentLoginRequestsPanel() {
  const [rows, setRows] = useState<AgentLoginRequestOut[]>([])
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listMyAgentLoginRequests()
      .then(setRows)
      .catch(() => {})
  }, [])

  const decide = async (id: number, accept: boolean) => {
    setBusy(id)
    setError(null)
    try {
      await decideMyAgentLoginRequest(id, accept)
      setRows((r) => r.filter((x) => x.id !== id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(null)
    }
  }

  if (rows.length === 0) return null
  return (
    <div className="rounded-xl border border-warning/30 bg-card p-5 space-y-3" data-testid="agent-login-requests">
      <h2 className="text-sm font-semibold text-foreground">Agents asking to use your account</h2>
      <p className="text-xs text-muted-foreground">
        Accepting makes your canopy account the agent&rsquo;s own login: its token then signs in as you,
        with everything you can reach. Decline unless this account was made for the agent.
      </p>
      <table className="w-full text-[12px]">
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-border">
              <td className="py-1.5 text-foreground">
                <span className="font-medium">{r.agent_name || r.agent_slug}</span>{' '}
                <span className="text-muted-foreground">({r.workspace_id})</span>
              </td>
              <td className="py-1.5 text-muted-foreground">asked by {r.requested_by_email || 'an admin'}</td>
              <td className="py-1.5 text-right">
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void decide(r.id, false)}
                  className="mr-2 rounded-md border border-input px-2 py-0.5 text-[11px] text-foreground disabled:opacity-40"
                >
                  Decline
                </button>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void decide(r.id, true)}
                  className="rounded-md bg-primary px-2 py-0.5 text-[11px] font-medium text-primary-foreground disabled:opacity-40"
                >
                  Accept
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <p className="text-[12px] text-destructive" role="alert">{error}</p>}
    </div>
  )
}
