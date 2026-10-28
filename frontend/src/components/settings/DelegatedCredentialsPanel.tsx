import { useEffect, useState } from 'react'
import {
  listMyDelegatedCredentials,
  withdrawMyDelegatedCredential,
  type MyDelegatedCredential,
} from '@/api/delegatedCredentials'

const REVOKE: Record<string, string> = {
  github: 'https://github.com/settings/personal-access-tokens',
  google: 'https://myaccount.google.com/connections',
}

const PROVIDER: Record<string, string> = { github: 'GitHub', google: 'Google' }

function providerName(provider: string): string {
  return PROVIDER[provider] ?? provider
}

function account(row: MyDelegatedCredential): string {
  const id = row.identity as { login?: string; username?: string; email?: string }
  const who = id.login ? `@${id.login}` : id.username || id.email || ''
  const provider = providerName(row.provider)
  return who ? `${provider} as ${who}` : provider
}

/**
 * Your credentials delegated to agents: each standing credential from your own account
 * that an agent uses. Only the machines that run the agent can open it — nobody can read
 * it in canopy, you included. Withdraw takes it out of the agent's vault; revoking it at
 * the provider is the only way to recall a copy a machine kept.
 */
export function DelegatedCredentialsPanel() {
  const [rows, setRows] = useState<MyDelegatedCredential[] | null>(null)
  const [error, setError] = useState('')

  async function refresh() {
    const res = await listMyDelegatedCredentials()
    if (res) setRows(res)
    else setError('Could not load the credentials you delegated.')
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function onWithdraw(row: MyDelegatedCredential) {
    if (await withdrawMyDelegatedCredential(row.id)) await refresh()
    else setError('Could not withdraw that credential.')
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-delegated">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Your credentials delegated to agents</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Credentials from your own accounts that an agent uses. Only the machines that run the agent
          can open one; nobody can read it in canopy, you included.
        </p>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {rows === null ? (
        !error && <p className="text-sm text-muted-foreground">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">You have delegated no credentials to an agent.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Agent</th>
              <th className="py-1 font-medium">Account</th>
              <th className="py-1 font-medium">Machines that can open it</th>
              <th className="py-1 font-medium">Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-border align-top">
                <td className="py-1.5 text-foreground-secondary">{r.agent_name || r.agent}</td>
                <td className="py-1.5 text-muted-foreground">{account(r)}</td>
                <td className="py-1.5 text-muted-foreground">
                  {(r.machines ?? []).join(', ') || 'none yet'}
                  {(r.waiting ?? []).length ? <> · waiting: {(r.waiting ?? []).join(', ')}</> : null}
                </td>
                <td className={`py-1.5 ${r.in_force ? 'text-muted-foreground' : 'text-destructive'}`}>
                  {r.in_force ? r.label.split(' · ').slice(2).join(' · ') || 'In use' : 'Not used: you are no longer its admin'}
                </td>
                <td className="py-1.5 text-right whitespace-nowrap">
                  <button
                    type="button"
                    onClick={() => void onWithdraw(r)}
                    className="text-destructive hover:underline"
                  >
                    Withdraw
                  </button>
                  {REVOKE[r.provider] && (
                    <a href={REVOKE[r.provider]} target="_blank" rel="noreferrer" className="ml-2 text-primary hover:underline">
                      Revoke at {providerName(r.provider)}
                    </a>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
