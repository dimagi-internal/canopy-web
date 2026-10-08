import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

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
  // The editor tier (docs/architecture/access.md): the whole agent, every turn manual.
  if (r.access === 'full' && r.manual_only) return 'Whole agent, manual only'
  if (r.access === 'full') return 'Whole agent'
  if (r.access === 'none') return 'No access'
  return `Only: ${r.capabilities.join(', ')}`
}

export function AgentAccessRoster({
  agentSlug,
  canManage,
  agentName = 'this agent',
  workspace,
}: {
  agentSlug: string
  canManage: boolean
  agentName?: string
  workspace?: string
}) {
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

  const toRow = (r: AgentAccessRowOut): PersonRow => {
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
          title={
            r.full_rule
              ? `Whole agent through the caller rule ${r.full_rule}`
              : r.manual_only
                ? 'A workspace editor: may change this agent and send it work, but every turn runs manual — outbound needs an admin'
                : undefined
          }
        >
          {accessText(r)}
        </span>,
      ],
    }
  }

  // Most rows are not a decision anyone made about THIS agent: they are the
  // workspace's membership, read through the workspace role. Drawing all of
  // them as an equal table buried the two or three that are (owner, granted
  // admins) under a dozen identical "Member" rows. So: what was set here first,
  // then the inherited rest summarised by what it reaches, with the full list
  // one click away (it is still where an admin is granted).
  const setHere = data.members.filter((r) => r.agent_role !== 'member')
  const inherited = data.members.filter((r) => r.agent_role === 'member')
  const groups = new Map<string, AgentAccessRowOut[]>()
  for (const r of inherited) groups.set(accessText(r), [...(groups.get(accessText(r)) ?? []), r])
  const outsiders = data.interface_published ? data.outsiders : []

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h4 className="m-0 mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Set on {agentName}
        </h4>
        <PeopleTable roleHeader="Role here" extraColumns={['Can reach']} actions={false} rows={setHere.map(toRow)} />
      </div>

      {inherited.length > 0 && (
        <div data-testid="agent-access-inherited">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3">
            <h4 className="m-0 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              From the workspace · {inherited.length} {inherited.length === 1 ? 'person' : 'people'}
            </h4>
            {workspace && (
              <Link to={`/w/${workspace}/settings/members`} className="text-[12px] text-primary hover:underline">
                Manage workspace members →
              </Link>
            )}
          </div>
          <ul data-testid="agent-access-groups" className="m-0 list-none divide-y divide-border rounded-lg border border-border p-0">
            {[...groups.entries()].map(([reach, rows]) => (
              <li key={reach} className="flex flex-wrap items-baseline gap-x-3 px-3 py-2 text-[12px]">
                <span className="font-medium text-foreground">{reach}</span>
                <span className="min-w-0 flex-1 text-muted-foreground">
                  {rows.map((r) => r.name || r.email).join(', ')}
                </span>
              </li>
            ))}
          </ul>
          <details className="mt-2">
            <summary className="cursor-pointer text-[12px] text-muted-foreground hover:text-foreground">
              Show all {inherited.length}
              {canManage ? ' (make someone an admin)' : ''}
            </summary>
            <div className="mt-2">
              <PeopleTable
                roleHeader="Role here"
                extraColumns={['Can reach']}
                actions={false}
                rows={inherited.map(toRow)}
              />
            </div>
          </details>
        </div>
      )}

      {outsiders.length > 0 && (
        <div className="text-[12px] text-foreground-secondary">
          <span className="font-medium text-foreground">Outside the workspace: </span>
          {outsiders.map((o, i) => (
            <span key={`${o.caller}-${o.capability ?? 'full'}`}>
              {i > 0 && ', '}
              <code className="text-[11px]">{o.caller}</code> → {o.access === 'full' ? 'whole agent' : `only ${o.capability}`}
            </span>
          ))}
        </div>
      )}

      <p className="m-0 text-[11px] text-muted-foreground">
        Workspace owners are always admins here. Only the owner and admins can run {agentName} in auto; everyone
        else's turns wait for approval. Workspace admins manage the workspace, not its agents.
      </p>
      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
