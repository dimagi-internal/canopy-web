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
  type AccessRoleOut,
  type AccessListOut,
  type AgentAccessOut,
} from '@/api/agents'
import { scopedPath } from '@/lib/scopedLinks'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { AccessLineForm } from './AccessLineForm'
import { ADDRESS_ONLY_KINDS, assignable, canText, lineWhat, roleWord } from './accessWords'
import { AgentAccessPeople } from './AgentAccessPeople'
import { AgentAdmins } from './AgentAdmins'
import { Td, Tr } from '@/components/DataTable'

// WHO HAS WHICH ROLE ON THIS AGENT — one list.
//
// An agent has ONE access list of (who, role) lines (docs/superpowers/specs/
// 2026-10-30-agent-roles-and-access-list-design.md, roles in 2026-11-02-agent-permissions-and-roles-design.md).
// Roles: read only < read & write < editor < agent admin < owner; a line may instead open one
// host site. This agent's own lines are changed here by its agent admins; lines
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
  // Advice from the server about a line that was added but can never match as written.
  const [notice, setNotice] = useState<string | null>(null)

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
        {[...data.roles].reverse().map((r) => (
          <div key={r.role} className="contents">
            <dt className="font-medium text-foreground">{roleWord(r.role, data.roles)}</dt>
            <dd className="m-0 text-foreground-secondary">
              {canText(r.role, data.roles)}
              {r.description ? ` · ${r.description}` : ''}
            </dd>
          </div>
        ))}
      </dl>

      <div>
        <h4 className="m-0 mb-1 text-[12px] font-medium text-foreground">Who has access</h4>
        <table className="w-full text-[12px]" data-testid="access-lines">
          <tbody>
            {data.implicit.map((i) => (
              <Tr rule="bottom-soft" key={`${i.role}:${i.who}`} data-testid="access-line-implicit">
                <Td density="compact-gap" tone="foreground">{i.who}</Td>
                <Td density="compact-gap" tone="foreground">{roleWord(i.role, data.roles)}</Td>
                <Td tone="muted">{i.why} · not a line, cannot be removed here</Td>
              </Tr>
            ))}
            {own.map((l) => (
              <OwnLine
                key={l.id}
                line={l}
                roles={data.roles}
                editable={data.can_edit}
                busy={busy}
                onPatch={(patch) => void act(() => changeAccessLine(agentSlug, l.id, patch))}
                onRemove={() => void act(() => removeAccessLine(agentSlug, l.id))}
              />
            ))}
            {inherited.map((l) => (
              <Tr rule="bottom-soft" key={`ws-${l.id}`} data-testid="access-line-inherited">
                <Td density="compact-gap" tone="foreground">{l.who}</Td>
                <Td density="compact-gap" tone="foreground">{lineWhat(l, data.roles)}</Td>
                <Td tone="muted">
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
                </Td>
              </Tr>
            ))}
            {own.length === 0 && inherited.length === 0 && data.implicit.length === 0 && (
              <tr>
                <Td tone="muted">Nobody yet.</Td>
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
            hostSites={data.host_sites}
            onAdd={async (line) => {
              const added = await addAccessLine(agentSlug, line)
              setError(null)
              setNotice(added.warning || null)
              await load()
            }}
          />
          {notice && (
            <p role="status" className="m-0 mt-2 text-[12px] text-warning" data-testid="access-line-warning">
              {notice}
            </p>
          )}
        </div>
      )}

      <AgentAdmins
        agentSlug={agentSlug}
        agentName={agentName}
        members={roster.members}
        canManage={canManageAdmins}
        onChanged={load}
      />

      <AgentAccessPeople members={roster.members} roles={data.roles} />
    </div>
  )
}

function OwnLine({
  line,
  roles,
  editable,
  busy,
  onPatch,
  onRemove,
}: {
  line: AccessLineOut
  roles: readonly AccessRoleOut[]
  editable: boolean
  busy: boolean
  onPatch: (patch: { role?: string; manual_only?: boolean }) => void
  onRemove: () => void
}) {
  const addressOnly = ADDRESS_ONLY_KINDS.includes(line.kind as never)
  return (
    <Tr rule="bottom-soft" data-testid="access-line">
      <Td density="compact-gap" tone="foreground">{line.who}</Td>
      <Td density="compact-gap">
        {editable && line.role ? (
          <select
            value={line.role}
            disabled={busy}
            aria-label={`Role for ${line.who}`}
            onChange={(e) => onPatch({ role: e.target.value })}
            className="rounded-md border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
          >
            {assignable(roles).map((r) => (
              <option key={r.role} value={r.role} disabled={r.contains_write && addressOnly}>
                {roleWord(r.role, roles)}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-foreground">{lineWhat(line, roles)}</span>
        )}
      </Td>
      <Td tone="muted">
        {editable ? (
          <span className="flex flex-wrap items-center gap-3">
            {line.role && (
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
            )}
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
      </Td>
    </Tr>
  )
}
