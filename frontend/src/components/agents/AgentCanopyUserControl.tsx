import { useState } from 'react'
import { linkAgentCanopyUser, type AgentDetailOut } from '@/api/agents'
import { listMembers, type MemberOut } from '@/api/workspaces'

type CanopyUser = AgentDetailOut['canopy_user']

// Which canopy user this agent IS — the account its own token signs in as
// (`ace@dimagi-ai.com` for ACE). Picked from the workspace's members, because
// the link is to a real account, not to an address. One user is one agent
// instance; the server says which instance already holds one. Browser-only on
// the server, like owner and admin changes.
export function AgentCanopyUserControl({
  agentSlug,
  workspace,
  initialUser,
  canEdit,
}: {
  agentSlug: string
  workspace: string
  initialUser: CanopyUser
  canEdit: boolean
}) {
  const [linked, setLinked] = useState<CanopyUser>(initialUser)
  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const open = async () => {
    setError(null)
    try {
      setMembers(await listMembers(workspace))
      setChoice(linked ? String(linked.user_id) : '')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load workspace members')
    }
  }

  const save = async (userId: number | null) => {
    setBusy(true)
    setError(null)
    try {
      const detail = await linkAgentCanopyUser(agentSlug, userId)
      setLinked(detail.canopy_user)
      setMembers(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not change the canopy user')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <span data-testid="agent-canopy-user" className={linked ? 'text-foreground' : 'text-muted-foreground'}>
          {linked ? linked.email : 'Not linked'}
        </span>
        {canEdit && members === null && (
          <button
            type="button"
            onClick={() => void open()}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            {linked ? 'Change' : 'Link canopy user'}
          </button>
        )}
        {canEdit && members === null && linked && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void save(null)}
            className="text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40"
          >
            Unlink
          </button>
        )}
      </div>
      {members !== null && (
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={`canopy-user-${agentSlug}`} className="text-[12px] text-muted-foreground">
            Canopy user
          </label>
          <select
            id={`canopy-user-${agentSlug}`}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            className="rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
          >
            <option value="" disabled>
              Choose a member…
            </option>
            {members.map((m) => (
              <option key={m.user_id} value={m.user_id}>
                {m.email}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={busy || !choice}
            onClick={() => void save(Number(choice))}
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            Link
          </button>
          <button
            type="button"
            onClick={() => setMembers(null)}
            className="text-[12px] text-muted-foreground hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      )}
      {error && (
        <p className="text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
