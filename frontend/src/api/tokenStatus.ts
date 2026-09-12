import type { PersonalToken } from './tokens'

/**
 * What to show in the Status column. Order matters: a revoked token may ALSO
 * carry an expiry, and "revoked" is the answer that matters — it is the one
 * that means "this credential is dead right now".
 */
export function tokenStatus(t: Pick<PersonalToken, 'revoked_at' | 'expires_at'>): string {
  if (t.revoked_at) return 'revoked'
  if (t.expires_at) return `expires ${t.expires_at.slice(0, 10)}`
  return 'active'
}
