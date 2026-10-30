import type { components } from '@/api/generated'

export type CredRow = components['schemas']['AgentCredentialStatusOut']

/** One line of the status list that leads the Credentials section (#1314):
 *  each credential's detail section reports it up, so the one that needs a
 *  person is visible at the top instead of last on the page. */
export type CredStatus = { label: string; tone: 'success' | 'warning' | 'destructive' | 'muted' }

// Pure logic behind AgentCredentialsSection.
//
// WHAT THIS SCREEN IS NOT. It is not "is this agent provisioned". canopy-web
// can only say whether a secret has a value in the agent's Canopy vault, never
// whether it WORKS — that is a question only the box can answer, and a readiness
// drill is what answers it.
//
// So this reports rather than judges: what is stored, what is declared but has
// no value yet, and what nothing accounts for.

/** What the screen is FOR, in the operator's terms rather than the store's:
 *  separate what needs a person from what is merely true. */
export interface CredentialSections {
  /** Stored here but no longer declared — a live secret nothing accounts for. */
  orphans: CredRow[]
  /** Declared and stored in the vault. */
  storedHere: CredRow[]
  /** Declared but no value yet: the one thing here that needs a person. */
  notSet: CredRow[]
}

/** `implied` names refs the agent needs without declaring them — today only
 *  `gog-token` for an agent that HAS a mailbox. "Connect Google mailbox" stores
 *  that slot for an agent that declares no secrets (ada/echo/eva/hal), and calling
 *  the result an orphan "worth removing" would invite someone to delete the one
 *  working mailbox token. */
export function sections(rows: readonly CredRow[], implied: readonly string[] = []): CredentialSections {
  const byName = (a: CredRow, b: CredRow) => a.name.localeCompare(b.name)
  const declared = (r: CredRow) => r.declared || implied.includes(r.name)
  return {
    orphans: rows.filter((r) => !declared(r)).sort(byName),
    storedHere: rows.filter((r) => declared(r) && r.set).sort(byName),
    notSet: rows.filter((r) => declared(r) && !r.set).sort(byName),
  }
}
