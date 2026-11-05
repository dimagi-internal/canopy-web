import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'

import {
  changeAgentRole,
  createAgentRole,
  deleteAgentRole,
  listAgentRoles,
  resetAgentRole,
  type AgentRoleOut,
  type AgentRolesOut,
} from '@/api/agents'
import { ApiError } from '@/api/problem'
import { Td, Th, Tr } from '@/components/DataTable'

// The roles an access-list line can give (apps/agents/api_roles.py). Any member reads them, in words;
// a workspace admin defines and edits the ones that cannot drive or change an agent; the owner defines
// anything that does. The server says which (`can_edit`, `can_give_write`) and refuses the rest in its
// own words, shown beside the form. Every write takes a passkey touch (the API client asks for it).

const FIELD = 'rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground'
const SMALL_BTN = 'px-2 text-primary hover:underline disabled:opacity-40'

type Draft = {
  /** null: defining a new role. Otherwise the key of the role being edited. */
  editing: string | null
  key: string
  label: string
  description: string
  read: boolean
  write: boolean
  agentEdit: boolean
  sites: string
}

const BLANK: Draft = { editing: null, key: '', label: '', description: '', read: false, write: false, agentEdit: false, sites: '' }

/** A name as a key: lowercase letters, digits and underscores, starting with a letter. */
function slugOf(name: string): string {
  const s = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
  return /^[a-z]/.test(s) ? s.slice(0, 40) : s ? `r_${s}`.slice(0, 40) : ''
}

function draftFrom(role: AgentRoleOut, copy: boolean): Draft {
  const held = new Set(role.permissions)
  return {
    editing: copy ? null : role.key,
    key: copy ? slugOf(`${role.key}_copy`) : role.key,
    label: copy ? `${role.label} (copy)` : role.label,
    description: role.description,
    read: held.has('read'),
    write: held.has('write'),
    agentEdit: held.has('agent_edit'),
    sites: role.permissions.filter((p) => p.startsWith('use:') && p !== 'use:*').map((p) => p.slice(4)).join(', '),
  }
}

function permissionsOf(d: Draft): string[] {
  const sites = d.sites.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean).map((x) => `use:${x}`)
  return [...(d.read ? ['read'] : []), ...(d.write ? ['write'] : []), ...(d.agentEdit ? ['agent_edit'] : []), ...sites]
}

export function WorkspaceAgentRolesPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [data, setData] = useState<AgentRolesOut | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [keyTouched, setKeyTouched] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [reassign, setReassign] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    listAgentRoles(workspace)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load the roles'))
  }, [workspace])

  useEffect(() => {
    load()
  }, [load])

  const act = async (fn: () => Promise<unknown>, done?: () => void) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      done?.()
      load()
    } catch (e: unknown) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!draft) return
    const permissions = permissionsOf(draft)
    void act(
      () =>
        draft.editing === null
          ? createAgentRole(workspace, { key: draft.key, label: draft.label, description: draft.description, permissions })
          : changeAgentRole(workspace, draft.editing, { label: draft.label, description: draft.description, permissions }),
      () => setDraft(null),
    )
  }

  if (data === null) {
    return error ? (
      <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p>
    ) : (
      <div className="h-24 animate-pulse rounded-lg bg-muted" />
    )
  }

  const own = (r: AgentRoleOut) => r.defined_on === workspace
  const writeBlocked = !data.can_give_write
  const setWrite = (on: boolean) =>
    setDraft((d) => d && { ...d, write: on, read: on ? true : d.read, agentEdit: on ? d.agentEdit : false })
  const setAgentEdit = (on: boolean) =>
    setDraft((d) => d && { ...d, agentEdit: on, write: on ? true : d.write, read: on ? true : d.read })

  return (
    <div className="flex flex-col gap-5" data-testid="agent-roles">
      <p className="m-0 text-[12px] text-muted-foreground">
        A role is a name and what it lets someone do with an agent. Access lines give roles; editing a role
        changes what everyone holding it can do, from their next request. Owner and agent admin are not roles
        you give out: they come from who the agent belongs to.
      </p>

      <table className="w-full text-[12px]" data-testid="agent-roles-table">
        <thead className="text-left text-muted-foreground">
          <Tr rule="bottom">
            <Th density="compact-gap">Role</Th>
            <Th density="compact-gap">What it can do</Th>
            <Th density="compact-gap">Defined on</Th>
            <Th density="compact-gap">In use</Th>
            <Th weight="inherit" />
          </Tr>
        </thead>
        <tbody>
          {data.roles.map((r) => (
            <Tr rule="bottom-soft" top key={r.key} data-testid={`agent-role-${r.key}`}>
              <Td density="cozy-gap" tone="foreground">
                {r.label}
                <span className="ml-1 text-[11px] text-muted-foreground">{r.key}</span>
                {r.builtin && (
                  <span className="ml-1 rounded border border-border px-1 text-[11px] text-muted-foreground">built in</span>
                )}
              </Td>
              <Td density="cozy-gap" tone="secondary">
                {r.can.length ? `Can ${r.can.join(', ')}` : ''}
                {r.description ? <span className="block text-muted-foreground">{r.description}</span> : null}
              </Td>
              <Td density="cozy-gap" tone="muted">{r.defined_on}</Td>
              <Td density="cozy-gap" tone="muted" nowrap>
                {r.in_use.lines === 0
                  ? 'not used'
                  : `${r.in_use.lines} line${r.in_use.lines === 1 ? '' : 's'} · ${r.in_use.agents} agent${r.in_use.agents === 1 ? '' : 's'}${r.in_use.defaults ? ` · ${r.in_use.defaults} default${r.in_use.defaults === 1 ? '' : 's'}` : ''}`}
              </Td>
              <Td density="cozy" nowrap align="right">
                {data.can_edit && (
                  <>
                    {r.can_edit && own(r) && (
                      <button type="button" className={SMALL_BTN} disabled={busy}
                        aria-label={`Edit ${r.label}`}
                        onClick={() => { setDraft(draftFrom(r, false)); setKeyTouched(true) }}>
                        Edit
                      </button>
                    )}
                    <button type="button" className={SMALL_BTN} disabled={busy}
                      aria-label={`Copy ${r.label}`}
                      onClick={() => { setDraft(draftFrom(r, true)); setKeyTouched(false) }}>
                      Copy
                    </button>
                    {r.builtin && own(r) && (
                      <button type="button" className={SMALL_BTN} disabled={busy}
                        aria-label={`Reset ${r.label} to its defaults`}
                        onClick={() => void act(() => resetAgentRole(workspace, r.key))}>
                        Reset
                      </button>
                    )}
                    {!r.builtin && own(r) && (
                      <button type="button" className="px-2 text-destructive hover:underline disabled:opacity-40"
                        disabled={busy} aria-label={`Delete ${r.label}`}
                        onClick={() => { setDeleting(r.key); setReassign('') }}>
                        Delete
                      </button>
                    )}
                  </>
                )}
              </Td>
            </Tr>
          ))}
          {data.fixed.map((f) => (
            <Tr rule="bottom-soft" top key={f.key} data-testid={`agent-role-fixed-${f.key}`}>
              <Td density="cozy-gap" tone="foreground">
                {f.label}
                <span className="ml-1 rounded border border-border px-1 text-[11px] text-muted-foreground">fixed</span>
              </Td>
              <Td density="cozy-gap" tone="secondary">{f.can.length ? `Can ${f.can.join(', ')}` : ''}</Td>
              <Td density="cozy-gap" tone="muted" colSpan={3}>Comes from who the agent belongs to: {f.why}. Not a line.</Td>
            </Tr>
          ))}
        </tbody>
      </table>

      {deleting && (
        <div className="rounded-md border border-border p-3 text-[12px]" data-testid="agent-role-delete">
          <p className="m-0 mb-2 text-foreground">
            Delete {deleting}? If lines still hold it, choose a role to move them to; otherwise the server refuses.
          </p>
          <select value={reassign} onChange={(e) => setReassign(e.target.value)} aria-label="Move its holders to" className={FIELD}>
            <option value="">Nobody holds it (no move)</option>
            {data.roles.filter((r) => r.key !== deleting).map((r) => (
              <option key={r.key} value={r.key}>{r.label}</option>
            ))}
          </select>
          <button type="button" disabled={busy} className="ml-2 rounded-md bg-destructive px-3 py-1 text-[12px] text-destructive-foreground disabled:opacity-40"
            onClick={() => void act(() => deleteAgentRole(workspace, deleting, reassign), () => setDeleting(null))}>
            Delete role
          </button>
          <button type="button" className="ml-2 text-muted-foreground hover:underline" onClick={() => setDeleting(null)}>
            Cancel
          </button>
        </div>
      )}

      {error && <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p>}

      {data.can_edit && !draft && (
        <div>
          <button type="button" className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90"
            onClick={() => { setDraft(BLANK); setKeyTouched(false) }}>
            New role
          </button>
        </div>
      )}
      {!data.can_edit && (
        <p className="m-0 text-[11px] text-muted-foreground">Only a workspace admin defines roles.</p>
      )}

      {draft && (
        <form onSubmit={submit} className="flex flex-col gap-2 rounded-md border border-border p-3" data-testid="agent-role-form"
          aria-label={draft.editing === null ? 'New role' : `Edit ${draft.editing}`}>
          <div className="flex flex-wrap items-center gap-2">
            <input value={draft.label} aria-label="Name" placeholder="Name, e.g. Reviewer" className={`${FIELD} w-56`}
              onChange={(e) => {
                const label = e.target.value
                setDraft((d) => d && { ...d, label, key: d.editing === null && !keyTouched ? slugOf(label) : d.key })
              }} />
            <input value={draft.key} aria-label="Key" placeholder="key" disabled={draft.editing !== null}
              className={`${FIELD} w-40 font-mono`}
              onChange={(e) => { setKeyTouched(true); setDraft((d) => d && { ...d, key: e.target.value }) }} />
            <input value={draft.description} aria-label="Description" placeholder="One sentence (optional)" className={`${FIELD} w-72`}
              onChange={(e) => setDraft((d) => d && { ...d, description: e.target.value })} />
          </div>
          <fieldset className="m-0 flex flex-col gap-0.5 border-0 p-0">
            <legend className="mb-0.5 p-0 text-[11px] text-muted-foreground">What it lets people do</legend>
            <label className="flex items-baseline gap-2 text-[12px] text-foreground">
              <input type="checkbox" checked={draft.read} disabled={draft.write}
                onChange={(e) => setDraft((d) => d && { ...d, read: e.target.checked })} />
              <span>Be answered in read mode</span>
              {draft.write && <span className="text-muted-foreground">(included: driving the agent includes it)</span>}
            </label>
            <label className={`flex items-baseline gap-2 text-[12px] ${writeBlocked ? 'text-muted-foreground' : 'text-foreground'}`}>
              <input type="checkbox" checked={draft.write} disabled={writeBlocked || draft.agentEdit}
                onChange={(e) => setWrite(e.target.checked)} />
              <span>Drive the whole agent</span>
              <span className="text-muted-foreground">
                {writeBlocked ? '(only a workspace owner can define a role that does)' : 'includes read mode and every connected site'}
              </span>
            </label>
            <label className={`flex items-baseline gap-2 text-[12px] ${writeBlocked ? 'text-muted-foreground' : 'text-foreground'}`}>
              <input type="checkbox" checked={draft.agentEdit} disabled={writeBlocked}
                onChange={(e) => setAgentEdit(e.target.checked)} />
              <span>Change its skills and settings</span>
              <span className="text-muted-foreground">includes driving the agent</span>
            </label>
            <label className="flex items-baseline gap-2 text-[12px] text-foreground">
              <span>Use only from these connected sites</span>
              <input value={draft.sites} aria-label="Connected sites" placeholder="connect, report" className={`${FIELD} w-56`}
                onChange={(e) => setDraft((d) => d && { ...d, sites: e.target.value })} />
              <span className="text-muted-foreground">the names of an agent's connected sites</span>
            </label>
          </fieldset>
          <p className="m-0 text-[11px] text-muted-foreground">
            A role that drives or changes an agent can never go on a line that names people only by their address.
          </p>
          <div className="flex items-center gap-3">
            <button type="submit" disabled={busy || !draft.label.trim() || !draft.key.trim() || permissionsOf(draft).length === 0}
              className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40">
              {busy ? 'Saving…' : draft.editing === null ? 'Create role' : 'Save role'}
            </button>
            <button type="button" className="text-[12px] text-muted-foreground hover:underline" onClick={() => setDraft(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
