import { useState } from 'react'
import { linkAgentCanopyUser, type AgentDetailOut } from '@/api/agents'

type CanopyUser = AgentDetailOut['canopy_user']
type LinkRequest = AgentDetailOut['canopy_user_request']

// Which canopy account this agent IS — the account its own token signs in as. It is
// ALWAYS the account at the agent's own email, never any other, so there is nothing to
// pick: "Link" links that account (canopy creates it when there is none), "Unlink"
// unlinks it. An existing account at that email is not linked on the spot — canopy
// can't tell an agent's mailbox from a person's, so its person accepts under Settings.
// The agent's owner or an admin, here or over MCP.
export function AgentCanopyUserControl({
  agentSlug,
  agentEmail = '',
  initialUser,
  initialRequest = null,
  canEdit,
}: {
  agentSlug: string
  agentEmail?: string
  workspace?: string
  initialUser: CanopyUser
  initialRequest?: LinkRequest
  canEdit: boolean
}) {
  const [linked, setLinked] = useState<CanopyUser>(initialUser)
  const [pending, setPending] = useState<LinkRequest>(initialRequest)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async (link: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const detail = await linkAgentCanopyUser(agentSlug, link)
      setLinked(detail.canopy_user)
      setPending(detail.canopy_user_request ?? null)
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
        {canEdit && !linked && (
          <button
            type="button"
            disabled={busy || !agentEmail}
            onClick={() => void save(true)}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary disabled:opacity-40"
          >
            {agentEmail ? `Link ${agentEmail}` : 'Set its email to link a login'}
          </button>
        )}
        {canEdit && linked && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void save(false)}
            className="text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40"
          >
            Unlink
          </button>
        )}
      </div>
      {pending && (
        <p data-testid="agent-canopy-user-pending" className="text-[12px] text-muted-foreground">
          Waiting for {pending.user_email} to accept — the agent would sign in as that account,
          so only its person can say yes (Settings, in their browser).
        </p>
      )}
      {error && (
        <p className="text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
