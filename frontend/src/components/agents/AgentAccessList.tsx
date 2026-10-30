import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  addAccessLine,
  changeAccessLine,
  getAccessList,
  getAgentAccess,
  removeAccessLine,
  setInheritWorkspaceDefaults,
  type AccessLineOut,
  type AccessListOut,
  type AgentAccessOut,
} from '@/api/agents'
import { scopedPath } from '@/lib/scopedLinks'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { AccessLineForm } from './AccessLineForm'
import { ADDABLE_ROLES, ADDRESS_ONLY_KINDS, ascending, canText, roleWord } from './accessWords'
import { AgentAccessPeople } from './AgentAccessPeople'
import { AgentAdmins } from './AgentAdmins'

// WHO HAS WHICH ROLE ON THIS AGENT — one list.
//
// An agent has ONE access list of (who, role) lines (docs/superpowers/specs/
// 2026-10-30-agent-roles-and-access-list-design.md). Roles: viewer < user < editor < agent
// admin < owner. This agent's own lines are changed here by its agent admins; lines
// inherited from a workspace are read-only (changed on the workspace); the owner and the
// agent admins hold their role without a line. The server decides every rule (a refusal is
// shown next to what was tried); this draws it.
export function AgentAccessList({
  agentSlug,
  agentName,
  workspace,
  canManageAdmins,
}: {
  agentSlug: string
  agentName: string
  workspace?: string
  canManageAdmins: boolean
}) {
  const { workspaces } = useWorkspace()
  const [data, setData] = useState<AccessListOut | null>(null)
  const [roster, setRoster] = useState<AgentAccessOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [list, people] = await Promise.all([getAccessList(agentSlug), getAgentAccess(agentSlug)])
      setData(list)
      setRoster(people)
    } catch (e: unknown) {
      setLoadError(e instanceof Error ? e.message : 'Could not load who has access')
    }
  }, [agentSlug])

  useEffect(() => {
    void load()
  }, [load])

  // One change at a time; a refusal (422) shows beside the list, in the server's words.
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  if (data === null || roster === null) {
    return loadError ? (
      <p role="alert" className="m-0 text-[12px] text-destructive">
        {loadError}
      </p>
    ) : (
      <div className="h-24 animate-pulse rounded-lg bg-muted" />
    )
  }

  const own = data.lines.filter((l) => l.scope === 'agent')
  const inherited = data.lines.filter((l) => l.scope === 'workspace')
  const home = workspace ?? ''
  const homeName = workspaces.find((w) => w.slug === home)?.display_name ?? home

  return (
    <div className="flex flex-col gap-5" data-testid="agent-access-list">
      <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-0.5 text-[12px]" data-testid="access-legend">
        {ascending(data.roles).map((r) => (
          <div key={r.role} className="contents">
            <dt className="font-medium text-foreground">{roleWord(r.role)}</dt>
            <dd className="m-0 text-foreground-secondary">{canText(r.role, data.roles)}</dd>
          </div>
        ))}
      </dl>

      {!data.uses_access_list && (
        <p className="m-0 text-[12px] text-muted-foreground">
          {agentName} has not moved onto this list yet: the rules it published before (see Doors) still let people in
          too. A line here adds to them.
        </p>
      )}

      <div>
        <h4 className="m-0 mb-1 text-[12px] font-medium text-foreground">Who has access</h4>
        <table className="w-full text-[12px]" data-testid="access-lines">
          <tbody>
            {data.implicit.map((i) => (
              <tr key={`${i.role}:${i.who}`} data-testid="access-line-implicit" className="border-b border-border/50">
                <td className="py-1 pr-3 text-foreground">{i.who}</td>
                <td className="py-1 pr-3 text-foreground">{roleWord(i.role)}</td>
                <td className="py-1 text-muted-foreground">{i.why} · not a line, cannot be removed here</td>
              </tr>
            ))}
            {own.map((l) => (
              <OwnLine
                key={l.id}
                line={l}
                editable={data.can_edit}
                busy={busy}
                onPatch={(patch) => void act(() => changeAccessLine(agentSlug, l.id, patch))}
                onRemove={() => void act(() => removeAccessLine(agentSlug, l.id))}
              />
            ))}
            {inherited.map((l) => (
              <tr key={`ws-${l.id}`} data-testid="access-line-inherited" className="border-b border-border/50">
                <td className="py-1 pr-3 text-foreground">{l.who}</td>
                <td className="py-1 pr-3 text-foreground">{roleWord(l.role)}</td>
                <td className="py-1 text-muted-foreground">
                  set on {l.scope_workspace}
                  {l.manual_only ? ' · needs approval before anything goes out' : ''}
                  {l.scope_workspace && (
                    <>
                      {' · '}
                      <Link to={scopedPath(l.scope_workspace, '/settings/members')} className="text-primary hover:underline">
                        change there
                      </Link>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {own.length === 0 && inherited.length === 0 && data.implicit.length === 0 && (
              <tr>
                <td className="py-1 text-muted-foreground">Nobody yet.</td>
              </tr>
            )}
          </tbody>
        </table>
        {error && (
          <p role="alert" className="m-0 mt-1 text-[12px] text-destructive">
            {error}
          </p>
        )}
      </div>

      <label className="flex items-center gap-2 text-[12px] text-foreground">
        <input
          type="checkbox"
          checked={data.inherit_workspace_defaults}
          disabled={!data.can_edit || busy}
          onChange={(e) => void act(() => setInheritWorkspaceDefaults(agentSlug, e.target.checked))}
        />
        Also take {homeName || 'the workspace'}&rsquo;s default access
      </label>

      {data.can_edit && (
        <div>
          <h4 className="m-0 mb-1 text-[12px] font-medium text-foreground">Add access</h4>
          <AccessLineForm
            roles={data.roles}
            workspaces={workspaces.map((w) => [w.slug, w.display_name] as [string, string])}
            onAdd={async (line) => {
              await addAccessLine(agentSlug, line)
              setError(null)
              await load()
            }}
          />
        </div>
      )}

      <AgentAdmins
        agentSlug={agentSlug}
        agentName={agentName}
        members={roster.members}
        canManage={canManageAdmins}
        onChanged={load}
      />

      <AgentAccessPeople members={roster.members} />
    </div>
  )
}

function OwnLine({
  line,
  editable,
  busy,
  onPatch,
  onRemove,
}: {
  line: AccessLineOut
  editable: boolean
  busy: boolean
  onPatch: (patch: { role?: 'viewer' | 'user' | 'editor'; manual_only?: boolean }) => void
  onRemove: () => void
}) {
  const addressOnly = ADDRESS_ONLY_KINDS.includes(line.kind as never)
  return (
    <tr data-testid="access-line" className="border-b border-border/50">
      <td className="py-1 pr-3 text-foreground">{line.who}</td>
      <td className="py-1 pr-3">
        {editable ? (
          <select
            value={line.role}
            disabled={busy}
            aria-label={`Role for ${line.who}`}
            onChange={(e) => onPatch({ role: e.target.value as 'viewer' | 'user' | 'editor' })}
            className="rounded-md border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
          >
            {ADDABLE_ROLES.map((r) => (
              <option key={r} value={r} disabled={r === 'editor' && addressOnly}>
                {roleWord(r)}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-foreground">{roleWord(line.role)}</span>
        )}
      </td>
      <td className="py-1 text-muted-foreground">
        {editable ? (
          <span className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={line.manual_only}
                disabled={busy}
                aria-label={`Needs approval before anything goes out for ${line.who}`}
                onChange={(e) => onPatch({ manual_only: e.target.checked })}
              />
              Needs approval before anything goes out
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={onRemove}
              aria-label={`Remove ${line.who}`}
              className="rounded border border-input px-1.5 text-[11px] text-muted-foreground hover:border-destructive hover:text-destructive disabled:opacity-40"
            >
              Remove
            </button>
          </span>
        ) : (
          <span>set on this agent{line.manual_only ? ' · needs approval before anything goes out' : ''}</span>
        )}
      </td>
    </tr>
  )
}
