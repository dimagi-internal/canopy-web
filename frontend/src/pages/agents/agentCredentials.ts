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

/** What the screen is FOR, in the operator's terms rather than the store's.
 *
 *  The first version listed all 45 declared refs as equal rows, each with an
 *  empty marker and a "paste to set" box. Every one of those rows was fine —
 *  they resolve from 1Password on the box, which is the intended arrangement —
 *  but forty-five empty inputs read as forty-five things to type. The one row
 *  that genuinely needed a human sat at the top looking like all the others.
 *  Jonathan's reaction on 2026-09-06 was "I'm very confused by this screen",
 *  which is the correct reaction to it.
 *
 *  So: separate what needs a person from what is merely true. */
export interface CredentialSections {
  /** Stored here but no longer declared — a live secret nothing accounts for. */
  orphans: CredRow[]
  /** Declared and stored in canopy-web: what this page actually manages. */
  storedHere: CredRow[]
  /** Declared, resolved from the vault on the box. Correct, and not a to-do. */
  fromVault: CredRow[]
}

export function sections(rows: readonly CredRow[]): CredentialSections {
  const byName = (a: CredRow, b: CredRow) => a.name.localeCompare(b.name)
  return {
    orphans: rows.filter((r) => !r.declared).sort(byName),
    storedHere: rows.filter((r) => r.declared && r.set).sort(byName),
    fromVault: rows.filter((r) => r.declared && !r.set).sort(byName),
  }
}

/** One line saying whether anything is wanted from the reader.
 *  Deliberately not a count of what canopy-web holds: "stores 0 of 45" is a fact
 *  about the store, and the reader wants a fact about their afternoon. */
export function headline(rows: readonly CredRow[]): string {
  const s = sections(rows)
  if (s.orphans.length > 0) {
    return `${s.orphans.length} stored secret(s) nothing declares any more — worth removing.`
  }
  if (s.storedHere.length === 0 && s.fromVault.length > 0) {
    return `Nothing here needs you. All ${s.fromVault.length} of this agent's secrets resolve from 1Password on the box.`
  }
  return `${s.storedHere.length} stored here; ${s.fromVault.length} resolve from 1Password on the box.`
}
