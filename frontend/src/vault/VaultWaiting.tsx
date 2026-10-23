import { useAuth } from '@/auth/AuthProvider'

// Who is waiting for a vault's key, and who can pass it on. A member can open the
// vault only once a copy of its key reaches them: a keeper (a runner holding it)
// passes it on by itself while online; a person holding it can grant it with one
// passkey touch, and is emailed after someone has waited 15 minutes.

type Member = { readonly email: string; readonly has_vault_key: boolean; readonly has_copy?: boolean }
type Holders = { readonly has_key?: boolean; readonly people?: readonly string[]; readonly keepers?: readonly string[] }

/** The short status after a member's sources: nothing when they can open it. */
export function memberWaiting(m: Member, holders: Holders | undefined): string | null {
  if (!m.has_vault_key) return 'hasn’t set up their vault key'
  if (holders?.has_key && !m.has_copy) return 'waiting for the key'
  return null
}

function list(names: readonly string[]): string {
  return names.length <= 2 ? names.join(' and ') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`
}

/** One line under the members table saying how the waiting get in — or that nobody can let them. */
export function VaultWaitingNote({ members, holders, testId }:
    { members: readonly Member[]; holders: Holders | undefined; testId: string }) {
  const { user } = useAuth()
  if (!holders?.has_key) return null
  const waiting = members.filter((m) => m.has_vault_key && !m.has_copy)
  if (waiting.length === 0) return null
  const me = waiting.some((m) => user?.email && m.email.toLowerCase() === user.email.toLowerCase())
  const keepers = holders.keepers ?? []
  const people = holders.people ?? []
  const who = me ? 'You are waiting for this vault’s key.' : `${list(waiting.map((m) => m.email))} ${waiting.length === 1 ? 'is' : 'are'} waiting for this vault’s key.`
  let how: string
  if (keepers.length) {
    how = `${list(keepers)} ${keepers.length === 1 ? 'passes' : 'pass'} it on by itself within a couple of minutes while online` +
      (people.length ? `; otherwise ${list(people)} can grant it, and ${people.length === 1 ? 'is' : 'are'} emailed after 15 minutes.` : '.')
  } else if (people.length) {
    how = `No runner holding it is set to pass it on, so ${list(people)} must grant it (one passkey touch), and ${people.length === 1 ? 'is' : 'are'} emailed after 15 minutes.`
  } else {
    how = 'Nobody who holds it can pass it on right now — open the vault with its recovery code, or add a runner that holds it.'
  }
  return (
    <p className={`mt-2 text-[12px] ${keepers.length || people.length ? 'text-muted-foreground' : 'text-warning'}`}
       data-testid={testId}>
      {who} {how}
    </p>
  )
}
