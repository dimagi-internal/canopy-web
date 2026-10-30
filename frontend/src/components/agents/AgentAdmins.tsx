import { useState } from 'react'

import { grantAgentAdmin, revokeAgentAdmin, type AgentAccessRowOut } from '@/api/agents'
import { roleAllows } from '@/lib/workspaceRoles'

// WHO IS THIS AGENT'S OWNER AND WHICH PEOPLE ARE ITS AGENT ADMINS.
//
// An agent's owner and its agent admins are not lines on the access list (an agent admin
// holds the agent's credentials, so it is granted by hand); this is where they are named,
// and where an agent admin is made or revoked. Who may grant and revoke is unchanged:
// `canManage` comes from the server. The roster rows come from the parent, which re-reads
// them (`onChanged`) after a change, since making someone an agent admin changes their access.
export function AgentAdmins({
  agentSlug,
  agentName,
  members,
  canManage,
  onChanged,
}: {
  agentSlug: string
  agentName: string
  members: readonly AgentAccessRowOut[]
  canManage: boolean
  onChanged: () => Promise<unknown> | void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState('')

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await onChanged()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not change agent admins')
    } finally {
      setBusy(false)
    }
  }

  // The agent's owner, and the workspace's owners (agent admins implicitly), are fixed.
  const isFixed = (r: AgentAccessRowOut) => r.agent_role === 'owner' || roleAllows(r.workspace_role, 'own')
  const owner = members.find((r) => r.agent_role === 'owner') ?? null
  const granted = members.filter((r) => r.agent_role === 'admin' && !isFixed(r))
  const grantable = members.filter((r) => r.agent_role === 'member')

  return (
    <div data-testid="agent-admins">
      <h4 className="m-0 text-[12px] font-medium text-foreground">Owner and agent admins of {agentName}</h4>
      <table className="mt-1 ml-3 text-[12px]">
        <tbody>
          <tr data-testid="agent-admin-row">
            <td className="w-24 py-0.5 pr-3 text-foreground-secondary">Owner</td>
            <td className="py-0.5 text-foreground">{owner ? owner.name : 'No owner'}</td>
            <td />
          </tr>
          {granted.map((r) => (
            <tr key={r.user_id} data-testid="agent-admin-row">
              <td className="w-24 py-0.5 pr-3 text-foreground-secondary">Agent admin</td>
              <td className="py-0.5 text-foreground">
                {r.name}
                {r.basis && <span className="text-muted-foreground"> · {r.basis}</span>}
              </td>
              <td className="py-0.5 pl-3">
                {canManage && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void act(() => revokeAgentAdmin(agentSlug, r.user_id))}
                    aria-label={`Remove ${r.email} as agent admin`}
                    className="rounded border border-input px-1.5 text-[11px] text-muted-foreground hover:border-destructive hover:text-destructive disabled:opacity-40"
                  >
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {canManage && grantable.length > 0 && (
        <div className="mt-1 ml-3 flex flex-wrap items-center gap-2">
          <select
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
            aria-label="Make an agent admin"
            className="rounded-md border border-input bg-input px-2 py-0.5 text-[12px] text-foreground"
          >
            <option value="">+ Make an agent admin…</option>
            {grantable.map((r) => (
              <option key={r.user_id} value={String(r.user_id)}>
                {r.email}
              </option>
            ))}
          </select>
          {adding && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                const id = Number(adding)
                setAdding('')
                void act(() => grantAgentAdmin(agentSlug, id))
              }}
              className="rounded-md bg-primary px-2.5 py-0.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
            >
              Make agent admin
            </button>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="m-0 mt-1 text-[12px] text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
