import { useState } from 'react'
import { transferAgentOwner, type AgentDetailOut } from '@/api/agents'
import { listMembers, type MemberOut } from '@/api/workspaces'

type Owner = AgentDetailOut['owner']

// Who operates this agent, and the one place ownership changes. Browser-only on
// the server side too: no agent or assistant can call it. Offered to a
// workspace owner or the agent's current owner (`canTransfer`).
export function AgentOwnerControl({
  agentSlug,
  workspace,
  initialOwner,
  canTransfer,
}: {
  agentSlug: string
  workspace: string
  initialOwner: Owner
  canTransfer: boolean
}) {
  const [owner, setOwner] = useState<Owner>(initialOwner)
  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const [choice, setChoice] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const open = async () => {
    setError(null)
    try {
      const list = await listMembers(workspace)
      setMembers(list)
      setChoice(owner ? String(owner.user_id) : '')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load workspace members')
    }
  }

  const save = async () => {
    if (!choice || busy) return
    setBusy(true)
    setError(null)
    try {
      const detail = await transferAgentOwner(agentSlug, Number(choice))
      setOwner(detail.owner)
      setMembers(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not transfer ownership')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <span data-testid="agent-owner" className="text-foreground">
          {owner ? `${owner.name}${owner.name !== owner.email ? ` (${owner.email})` : ''}` : 'No owner'}
        </span>
        {canTransfer && members === null && (
          <button
            type="button"
            onClick={() => void open()}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            {owner ? 'Transfer' : 'Assign owner'}
          </button>
        )}
      </div>
      {members !== null && (
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={`owner-${agentSlug}`} className="text-[12px] text-muted-foreground">
            New owner
          </label>
          <select
            id={`owner-${agentSlug}`}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            className="rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
          >
            <option value="" disabled>
              Choose a member…
            </option>
            {members.map((m) => (
              <option key={m.user_id} value={String(m.user_id)}>
                {m.email} ({m.role})
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!choice || busy || Number(choice) === owner?.user_id}
            onClick={() => void save()}
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy ? 'Saving…' : 'Save'}
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
