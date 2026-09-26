import { useState } from 'react'
import { setAgentLogin, type AgentDetailOut } from '@/api/agents'

type Login = AgentDetailOut['login']

// The canopy login that IS this agent — the account its own token belongs to.
// Without the link, canopy cannot tell the agent calling as itself from anyone
// else, so the checks that ask exactly that (secrets shared in its chats, the
// page a chat is on) refuse it. One login is one instance: of several copies of
// an agent, only one can be its mailbox's login. Browser-only on the server.
export function AgentLoginControl({
  agentSlug,
  initialLogin,
  canEdit,
}: {
  agentSlug: string
  initialLogin: Login
  canEdit: boolean
}) {
  const [login, setLogin] = useState<Login>(initialLogin)
  const [editing, setEditing] = useState(false)
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async (next: string) => {
    setBusy(true)
    setError(null)
    try {
      const detail = await setAgentLogin(agentSlug, next)
      setLogin(detail.login)
      setEditing(false)
      setEmail('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not change the login')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <span data-testid="agent-login" className={login ? 'text-foreground' : 'text-muted-foreground'}>
          {login ? login.email : 'Not linked'}
        </span>
        {canEdit && !editing && (
          <button
            type="button"
            onClick={() => {
              setEditing(true)
              setEmail(login?.email ?? '')
            }}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            {login ? 'Change' : 'Link a login'}
          </button>
        )}
        {canEdit && !editing && login && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void save('')}
            className="text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40"
          >
            Unlink
          </button>
        )}
      </div>
      {editing && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            aria-label="Login email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="agent@dimagi-ai.com"
            className="w-64 rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
          />
          <button
            type="button"
            disabled={busy || !email.trim()}
            onClick={() => void save(email.trim())}
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => setEditing(false)}
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
