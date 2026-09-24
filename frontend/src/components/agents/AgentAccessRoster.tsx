import { useCallback, useEffect, useState } from 'react'

import {
  getAgentAccess,
  grantAgentAdmin,
  revokeAgentAdmin,
  type AgentAccessOut,
  type AgentAccessRowOut,
} from '@/api/agents'

// EVERYONE'S ROLE ON THIS AGENT, in one table.
//
// Access to an agent is decided in four places — its owner, explicit admins,
// the workspace's owners (admins implicitly, listed nowhere else), and the
// published interface that confines everyone else — so reading any one of them
// answered a quarter of "what can this person do here". The server composes the
// whole answer (apps/agents/access.py) with the same rules a turn is decided
// by; this only draws it. Granting and revoking admin happen on the row, which
// is why the old separate admins list is gone.

const ROLE_LABEL: Record<AgentAccessRowOut['agent_role'], string> = {
  owner: 'Owner',
  admin: 'Admin',
  member: 'Member',
}

const ROLE_TONE: Record<AgentAccessRowOut['agent_role'], string> = {
  owner: 'bg-primary/10 text-primary border-primary/30',
  admin: 'bg-info/10 text-info border-info/30',
  member: 'bg-muted text-foreground-secondary border-border',
}

function accessText(r: AgentAccessRowOut): string {
  if (r.access === 'full') return 'Whole agent'
  if (r.access === 'none') return 'No access'
  return `Only: ${r.capabilities.join(', ')}`
}

export function AgentAccessRoster({ agentSlug, canManage }: { agentSlug: string; canManage: boolean }) {
  const [data, setData] = useState<AgentAccessOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await getAgentAccess(agentSlug))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load who has access')
    }
  }, [agentSlug])

  useEffect(() => {
    void load()
  }, [load])

  const change = async (r: AgentAccessRowOut, grant: boolean) => {
    if (busyId !== null) return
    setBusyId(r.user_id)
    setError(null)
    try {
      await (grant ? grantAgentAdmin(agentSlug, r.user_id) : revokeAgentAdmin(agentSlug, r.user_id))
      // Admin changes someone's access too, not just their role: reload the
      // whole answer rather than patching one cell.
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : grant ? 'Could not make admin' : 'Could not remove admin')
    } finally {
      setBusyId(null)
    }
  }

  if (data === null) {
    return error ? (
      <p role="alert" className="m-0 text-[12px] text-destructive">
        {error}
      </p>
    ) : (
      <div className="h-24 animate-pulse rounded-lg bg-muted" />
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-muted-foreground">
            <th className="py-1.5 font-medium">Person</th>
            <th className="py-1.5 font-medium">Role here</th>
            <th className="py-1.5 font-medium">Why</th>
            <th className="py-1.5 font-medium">Can reach</th>
            {canManage && <th className="py-1.5" />}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {data.members.map((r) => {
            const granted = r.agent_role === 'admin' && r.workspace_role !== 'owner'
            return (
              <tr key={r.user_id} data-testid="agent-access-row">
                <td className="py-1.5 pr-3 text-foreground">
                  {r.name}
                  {r.name !== r.email && <span className="text-muted-foreground"> ({r.email})</span>}
                  <div className="text-[11px] capitalize text-muted-foreground">workspace {r.workspace_role}</div>
                </td>
                <td className="py-1.5 pr-3">
                  <span className={`inline-block rounded border px-1.5 py-0.5 text-[11px] font-medium ${ROLE_TONE[r.agent_role]}`}>
                    {ROLE_LABEL[r.agent_role]}
                  </span>
                </td>
                <td className="py-1.5 pr-3 text-[12px] text-muted-foreground">{r.basis}</td>
                <td
                  className={`py-1.5 pr-3 text-[12px] ${r.access === 'none' ? 'text-warning' : 'text-foreground-secondary'}`}
                  title={r.full_rule ? `Whole agent through the caller rule ${r.full_rule}` : undefined}
                >
                  {accessText(r)}
                </td>
                {canManage && (
                  <td className="py-1.5 text-right">
                    {r.agent_role === 'member' && (
                      <button
                        type="button"
                        disabled={busyId !== null}
                        onClick={() => void change(r, true)}
                        aria-label={`Make ${r.email} an admin`}
                        className="text-[12px] text-muted-foreground underline hover:text-primary disabled:opacity-50"
                      >
                        Make admin
                      </button>
                    )}
                    {granted && (
                      <button
                        type="button"
                        disabled={busyId !== null}
                        onClick={() => void change(r, false)}
                        aria-label={`Remove ${r.email} as admin`}
                        className="text-[12px] text-muted-foreground underline hover:text-destructive disabled:opacity-50"
                      >
                        Remove admin
                      </button>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>

      <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px] text-foreground-secondary">
        <span className="font-medium text-foreground">Outside the workspace: </span>
        {!data.interface_published ? (
          <>no caller rules are published, so anyone who reaches this agent gets the whole agent.</>
        ) : data.outsiders.length === 0 ? (
          <>no one — the published caller rules name only workspace members.</>
        ) : (
          <ul className="m-0 mt-1 list-none p-0">
            {data.outsiders.map((o) => (
              <li key={`${o.caller}-${o.capability ?? 'full'}`}>
                <code className="text-[11px]">{o.caller}</code> →{' '}
                {o.access === 'full' ? 'whole agent' : `only ${o.capability}`}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-1 text-muted-foreground">Slack: {data.slack_enabled ? 'on' : 'off'}.</div>
      </div>

      <p className="m-0 text-[11px] text-muted-foreground">
        Owners and admins can change the agent and hold its keys. "Can reach" is what someone gets when signed in to
        canopy; a message by unverified email may reach less.
      </p>
      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
