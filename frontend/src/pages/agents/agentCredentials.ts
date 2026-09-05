import type { components } from '@/api/generated'

export type CredRow = components['schemas']['AgentCredentialStatusOut']

// Pure logic behind AgentCredentialsSection, extracted so it unit-tests without
// a renderer. The screen answers one question — "what is stopping this agent
// from running" — which today costs an SSH to a box and a `gog auth list`.

export interface Blockers {
  /** Declared refs with no value. These are what stop a turn. */
  missing: string[]
  /** Stored but no longer declared — a live secret nothing accounts for. */
  orphans: string[]
  /** Set, but still resolving out of 1Password rather than canopy-web. */
  stillInVault: string[]
  ready: boolean
  /** The agent declares nothing at all, which is NOT the same as ready. */
  undeclared: boolean
}

export function blockers(rows: readonly CredRow[]): Blockers {
  const declared = rows.filter((r) => r.declared)
  const missing = declared.filter((r) => !r.set).map((r) => r.name)
  return {
    missing,
    orphans: rows.filter((r) => !r.declared).map((r) => r.name),
    stillInVault: rows.filter((r) => r.set && r.source === '1password').map((r) => r.name),
    // An agent with zero declared refs is UNDECLARED, not provisioned. Calling
    // that "ready" would assert a box can run it, which nobody has established —
    // and it is the state every agent is in before someone writes a runtime.yaml.
    ready: declared.length > 0 && missing.length === 0,
    undeclared: declared.length === 0,
  }
}

/** Unset-and-required first (the answer to the question), then the rest, then
 *  orphans last — they need showing but are noise next to a blocker. */
export function groupRefs(rows: readonly CredRow[]): CredRow[] {
  const rank = (r: CredRow) => (!r.declared ? 2 : r.set ? 1 : 0)
  return [...rows].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
}
