import { useCallback, useEffect, useState } from 'react'

import {
  getAgentAccess,
  grantAgentAdmin,
  revokeAgentAdmin,
  type AgentAccessOut,
  type AgentAccessRowOut,
} from '@/api/agents'
import { PeopleTable, type PersonRow } from '@/components/people/PeopleTable'
import type { RoleOption } from '@/components/people/roles'
import { roleAllows } from '@/lib/workspaceRoles'

// EVERYONE'S ROLE ON THIS AGENT, in one table.
//
// Access to an agent is decided in four places — its owner, explicit admins,
// the workspace's owners (admins implicitly, listed nowhere else), and the
// published interface that confines everyone else — so reading any one of them
// answered a quarter of "what can this person do here". The server composes the
// whole answer (apps/agents/access.py) with the same rules a turn is decided
// by; this only draws it. Granting and revoking admin is the row's role
// dropdown (Member / Admin) — the same control every people surface uses.

// Owner is never a choice — it is who owns the agent — so a changeable row
// picks between these two, and Owner only ever renders fixed.
const ROLE_OPTIONS: RoleOption[] = [
  { value: 'member', label: 'Member' },
  { value: 'admin', label: 'Admin' },
]

const ROLE_LABEL: Record<AgentAccessRowOut['agent_role'], string> = {
  owner: 'Owner',
  admin: 'Admin',
  member: 'Member',
}

function accessText(r: AgentAccessRowOut): string {
  if (r.access === 'full') return 'Whole agent'
  if (r.access === 'none') return 'No access'
  return `Only: ${r.capabilities.join(', ')}`
}

export function AgentAccessRoster({ agentSlug, canManage }: { agentSlug: string; canManage: boolean }) {
  const [data, setData] = useState<AgentAccessOut | null>(null)
  const [error, setError] = useState<string | null>(null)

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

  // Rejects on failure; PeopleTable shows the error on the row.
  const change = async (r: AgentAccessRowOut, next: string) => {
    if (next === r.agent_role) return
    await (next === 'admin' ? grantAgentAdmin(agentSlug, r.user_id) : revokeAgentAdmin(agentSlug, r.user_id))
    // Admin changes someone's access too, not just their role: reload the
    // whole answer rather than patching one cell.
    await load()
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
      <PeopleTable
        roleHeader="Role here"
        extraColumns={['Can reach']}
        actions={false}
        rows={data.members.map((r): PersonRow => {
          // The agent's owner, and the workspace's owners (admins implicitly),
          // are fixed here; everyone else is a member or a granted admin.
          const fixed = r.agent_role === 'owner' || roleAllows(r.workspace_role, 'own')
          return {
            key: r.user_id,
            testId: 'agent-access-row',
            name: r.name,
            email: r.email,
            detail: <span className="capitalize">workspace {r.workspace_role}</span>,
            role: r.agent_role,
            roleLabel: ROLE_LABEL[r.agent_role],
            options: ROLE_OPTIONS,
            editable: canManage && !fixed,
            why: r.basis,
            onRoleChange: (next) => change(r, next),
            extra: [
              <span
                key="reach"
                className={`text-[12px] ${r.access === 'none' ? 'text-warning' : 'text-foreground-secondary'}`}
                title={r.full_rule ? `Whole agent through the caller rule ${r.full_rule}` : undefined}
              >
                {accessText(r)}
              </span>,
            ],
          }
        })}
      />

      <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px] text-foreground-secondary">
        <span className="font-medium text-foreground">Outside the workspace: </span>
        {!data.interface_published ? (
          <>no one — no caller rules are published, so only workspace members can reach this agent.</>
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
