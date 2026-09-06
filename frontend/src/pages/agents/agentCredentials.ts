import type { components } from '@/api/generated'

export type CredRow = components['schemas']['AgentCredentialStatusOut']

// Pure logic behind AgentCredentialsSection.
//
// WHAT THIS SCREEN IS NOT. It is not "is this agent provisioned". canopy-web
// cannot see 1Password, and by design it does not hold most of an agent's
// secrets — the vault does, reached on the box with the runner's 1Password
// service-account token (RunnerCredential.op_sa_token, set on the Runners tab).
//
// A first draft measured `selfContained` — "every declared secret is stored
// here" — and rendered it as the success state. That rewards copying all 45 of
// ACE's secrets into canopy-web: a second copy of every credential, free to
// drift from the vault, in a system that then becomes worth attacking. It also
// reported "45 blockers, this agent cannot run" about an agent running
// perfectly well, which is how a screen teaches people to ignore it — and the
// one genuinely dead credential then hides among the false alarms.
//
// So this reports rather than judges: what canopy-web holds, what it does not,
// and what nothing accounts for. Whether a secret actually WORKS is a question
// only the box can answer, and a readiness drill is what answers it.

export interface CredentialSummary {
  /** Refs the agent declares (orphans excluded). */
  declaredCount: number
  /** Declared and stored in canopy-web. */
  storedHere: string[]
  /** Declared, not stored here — expected to resolve from 1Password on the box. */
  fromVault: string[]
  /** Stored but no longer declared: a live secret nothing accounts for. */
  orphans: string[]
  /** The agent declares nothing, which is NOT the same as provisioned. */
  undeclared: boolean
}

export function summarize(rows: readonly CredRow[]): CredentialSummary {
  const declared = rows.filter((r) => r.declared)
  return {
    declaredCount: declared.length,
    storedHere: declared.filter((r) => r.set).map((r) => r.name),
    fromVault: declared.filter((r) => !r.set).map((r) => r.name),
    orphans: rows.filter((r) => !r.declared).map((r) => r.name),
    undeclared: declared.length === 0,
  }
}

/** Stored-here first (what this page actually manages), then vault-resolved,
 *  then orphans last — they need showing, but they are noise up top. */
export function groupRefs(rows: readonly CredRow[]): CredRow[] {
  const rank = (r: CredRow) => (!r.declared ? 2 : r.set ? 0 : 1)
  return [...rows].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
}
