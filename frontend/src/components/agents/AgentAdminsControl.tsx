import { useEffect, useState } from 'react'
import { grantAgentAdmin, listAgentAdmins, revokeAgentAdmin, type AgentAdminOut } from '@/api/agents'
import { listMembers, type MemberOut } from '@/api/workspaces'

// Who holds this agent's keys: its owner, plus members granted admin by name.
// An admin may set the agent's credentials and, as the rest of the
// who-is-asking work lands, reaches its full working session. Workspace owners
// are admins implicitly. Changes are browser-only on the server too.
export function AgentAdminsControl({
  agentSlug,
  workspace,
  canManage,
}: {
  agentSlug: string
  workspace: string
  canManage: boolean
}) {
  const [admins, setAdmins] = useState<AgentAdminOut[] | null>(null)
  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listAgentAdmins(agentSlug)
      .then((rows) => !cancelled && setAdmins(rows))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load admins'))
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  const run = async (fn: () => Promise<AgentAdminOut[]>, fallback: string) => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      setAdmins(await fn())
      setMembers(null)
      setChoice('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : fallback)
    } finally {
      setBusy(false)
    }
  }

  const open = async () => {
    setError(null)
    try {
      setMembers(await listMembers(workspace))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load workspace members')
    }
  }

  const held = new Set((admins ?? []).map((a) => a.user_id))
  const candidates = (members ?? []).filter((m) => !held.has(m.user_id) && m.role !== 'owner')

  return (
    <div className="flex flex-col gap-2">
      {admins === null && !error && <p className="m-0 text-[12px] text-muted-foreground">Loading…</p>}
      {admins !== null && (
        <table className="w-full text-[13px]">
          <tbody className="divide-y divide-border">
            {admins.map((a) => (
              <tr key={a.user_id} data-testid="agent-admin">
                <td className="py-1.5 text-foreground">
                  {a.name}
                  {a.name !== a.email && <span className="text-muted-foreground"> ({a.email})</span>}
                </td>
                <td className="py-1.5 text-[12px] text-muted-foreground">
                  {a.is_owner ? 'Owner' : a.granted_by_email ? `Granted by ${a.granted_by_email}` : 'Admin'}
                </td>
                <td className="py-1.5 text-right">
                  {canManage && !a.is_owner && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void run(() => revokeAgentAdmin(agentSlug, a.user_id), 'Could not revoke')}
                      className="text-[12px] text-muted-foreground underline hover:text-destructive disabled:opacity-50"
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="m-0 text-[11px] text-muted-foreground">Workspace owners are always admins.</p>
      {canManage && members === null && (
        <div>
          <button
            type="button"
            onClick={() => void open()}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            Add admin
          </button>
        </div>
      )}
      {members !== null && (
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={`admin-${agentSlug}`} className="text-[12px] text-muted-foreground">
            New admin
          </label>
          <select
            id={`admin-${agentSlug}`}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            className="rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
          >
            <option value="" disabled>
              {candidates.length ? 'Choose a member…' : 'Every member already is one'}
            </option>
            {candidates.map((m) => (
              <option key={m.user_id} value={String(m.user_id)}>
                {m.email} ({m.role})
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!choice || busy}
            onClick={() => void run(() => grantAgentAdmin(agentSlug, Number(choice)), 'Could not grant')}
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy ? 'Saving…' : 'Grant'}
          </button>
          <button type="button" onClick={() => setMembers(null)} className="text-[12px] text-muted-foreground underline">
            Cancel
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
