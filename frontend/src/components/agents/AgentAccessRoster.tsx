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

// EVERYONE'S ROLE ON THIS AGENT.
//
// Most of it is not set on the agent at all: nearly every row follows from the
// person's WORKSPACE role (#1314 — on Echo, 12 of 14 rows were the same
// inherited line). So the rule is drawn first, then only what IS set on this
// agent (its owner and granted admins), and the full per-person table sits
// behind an expander for the one question only it answers: "what does THIS
// person get".
//
// The server composes each person's answer (apps/agents/access.py) with the
// same rules a turn is decided by; this only draws it. Who may grant and revoke
// admin is unchanged: `canManage` comes from the server.

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
  agentName,
  workspace,
  canManage,
}: {
  agentSlug: string
  agentName?: string
  workspace?: string
  canManage: boolean
}) {
  const name = agentName ?? agentSlug
  const [data, setData] = useState<AgentAccessOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState('')

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

  // From the full table's role dropdown. Rejects on failure; PeopleTable shows
  // the error on the row.
  const change = async (r: AgentAccessRowOut, next: string) => {
    if (next === r.agent_role) return
    await (next === 'admin' ? grantAgentAdmin(agentSlug, r.user_id) : revokeAgentAdmin(agentSlug, r.user_id))
    // Admin changes someone's access too, not just their role: reload the
    // whole answer rather than patching one cell.
    await load()
  }

  // From the exception list's own Remove / Add admin.
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not change admins')
    } finally {
      setBusy(false)
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

  // The agent's owner, and the workspace's owners (admins implicitly), are
  // fixed; everyone else is a member or a granted admin.
  const isFixed = (r: AgentAccessRowOut) => r.agent_role === 'owner' || roleAllows(r.workspace_role, 'own')
  const owner = data.members.find((r) => r.agent_role === 'owner') ?? null
  const granted = data.members.filter((r) => r.agent_role === 'admin' && !isFixed(r))
  const grantable = data.members.filter((r) => r.agent_role === 'member')
  const count = data.members.length
  const people = `${count} ${count === 1 ? 'person' : 'people'}`

  // The rule access.decide applies by workspace role (docs/architecture/access.md).
  const legend: [string, string][] = [
    ['Workspace owners', `Admin of ${name}`],
    ['Editors & admins', `Can use ${name}; turns need approval`],
    ['Viewers', data.interface_published ? 'What the caller rules allow' : 'No access'],
  ]

  return (
    <div className="flex flex-col gap-4" data-testid="agent-access">
      <div data-testid="agent-access-inherited">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <h4 className="m-0 text-[12px] font-medium text-foreground">
            Inherited from the {workspace ? `${workspace} ` : ''}workspace ({people})
          </h4>
          {workspace && (
            <Link to={`/w/${workspace}/settings/members`} className="text-[11px] text-primary hover:underline">
              Manage in workspace →
            </Link>
          )}
        </div>
        <dl className="m-0 mt-1 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-0.5 pl-3 text-[12px]">
          {legend.map(([who, gets]) => (
            <div key={who} className="contents" data-testid="agent-access-legend">
              <dt className="text-foreground-secondary">{who}</dt>
              <dd className="m-0 text-foreground">→ {gets}</dd>
            </div>
          ))}
        </dl>
      </div>

      <div data-testid="agent-access-exceptions">
        <h4 className="m-0 text-[12px] font-medium text-foreground">Set on {name}</h4>
        <table className="mt-1 ml-3 text-[12px]">
          <tbody>
            <tr data-testid="agent-access-exception">
              <td className="w-16 py-0.5 pr-3 text-foreground-secondary">Owner</td>
              <td className="py-0.5 text-foreground">{owner ? owner.name : 'No owner'}</td>
              <td />
            </tr>
            {granted.map((r) => (
              <tr key={r.user_id} data-testid="agent-access-exception">
                <td className="w-16 py-0.5 pr-3 text-foreground-secondary">Admin</td>
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
                      aria-label={`Remove ${r.email} as admin`}
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
              aria-label="Add admin"
              className="rounded-md border border-input bg-input px-2 py-0.5 text-[12px] text-foreground"
            >
              <option value="">+ Add admin…</option>
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
                Make admin
              </button>
            )}
          </div>
        )}
      </div>

      <button
        type="button"
        onClick={() => setShowAll((v) => !v)}
        aria-expanded={showAll}
        className="self-start text-[12px] text-muted-foreground hover:text-foreground"
        data-testid="agent-access-show-all"
      >
        {showAll ? '▾ Hide' : '▸ Show'} all {people}
      </button>

      {showAll && (
        <PeopleTable
          roleHeader="Role here"
          extraColumns={['Can reach']}
          actions={false}
          rows={data.members.map(
            (r): PersonRow => ({
              key: r.user_id,
              testId: 'agent-access-row',
              name: r.name,
              email: r.email,
              detail: <span className="capitalize">workspace {r.workspace_role}</span>,
              role: r.agent_role,
              roleLabel: ROLE_LABEL[r.agent_role],
              options: ROLE_OPTIONS,
              editable: canManage && !isFixed(r),
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
            }),
          )}
        />
      )}

      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
