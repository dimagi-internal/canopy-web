// An agent's address on a canopy-hosted domain needs no sign-in: canopy-web routes it
// to the agent the moment the agent exists (apps/mail/domains.ensure_agent_route).
// Anywhere else (Gmail, Google Workspace mail) it is signed in with Google later.
import { useEffect, useState } from 'react'
import { listMailDomains } from '@/api/mail'

/** The workspace's verified canopy-hosted domains (its own and shared ones). */
export function useHostedDomains(workspace: string): string[] {
  const [names, setNames] = useState<string[]>([])
  useEffect(() => {
    if (!workspace) return
    let live = true
    listMailDomains(workspace)
      .then((r) => {
        if (live) setNames(r.items.filter((d) => d.status !== 'pending').map((d) => d.name.toLowerCase()))
      })
      .catch(() => undefined) // email off or no access: say nothing hosted
    return () => {
      live = false
    }
  }, [workspace])
  return names
}

export function hostedDomainOf(address: string, hosted: readonly string[]): string {
  const host = address.trim().toLowerCase().split('@')[1] ?? ''
  return host && hosted.includes(host) ? host : ''
}

/** What the mailbox field says under itself, given what is typed. */
export function mailboxHint(address: string, hosted: readonly string[]): string {
  if (hostedDomainOf(address, hosted)) return 'Canopy-hosted email — nothing to sign in. Mail to it becomes this agent’s turn.'
  if (address.trim()) return 'An address it sends and reads mail as. You sign it in with Google later.'
  return hosted.length
    ? `An address it sends and reads mail as. One @${hosted[0]} is canopy-hosted: nothing to sign in.`
    : 'An address it sends and reads mail as. You sign it in with Google later.'
}
