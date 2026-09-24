import { describe, expect, it } from 'vitest'

import type { InviteOut } from '@/api/workspaces'
import { emailOutcomeText, inviteExpiryLabel, isInviteOutstanding } from './workspaceInvites'

const NOW = Date.parse('2026-09-24T12:00:00Z')
const DAY = 86_400_000

function inv(overrides: Partial<InviteOut> = {}): InviteOut {
  return {
    id: 1, email: 'b@x.org', role: 'editor', token: 't',
    expires_at: new Date(NOW + 3 * DAY).toISOString(),
    accepted_at: null, revoked_at: null,
    ...overrides,
  }
}

describe('workspace invite helpers', () => {
  it('labels expiry in both directions', () => {
    expect(inviteExpiryLabel(inv(), NOW)).toBe('Expires in 3 days')
    expect(inviteExpiryLabel(inv({ expires_at: new Date(NOW + DAY).toISOString() }), NOW)).toBe('Expires in 1 day')
    expect(inviteExpiryLabel(inv({ expires_at: new Date(NOW - 2 * DAY).toISOString() }), NOW)).toBe('Expired 2 days ago')
  })

  it('an expired invite is still outstanding; accepted and revoked are not', () => {
    expect(isInviteOutstanding(inv({ expires_at: new Date(NOW - DAY).toISOString() }))).toBe(true)
    expect(isInviteOutstanding(inv({ accepted_at: new Date(NOW).toISOString() }))).toBe(false)
    expect(isInviteOutstanding(inv({ revoked_at: new Date(NOW).toISOString() }))).toBe(false)
  })

  it('only a sent email reads as delivered', () => {
    expect(emailOutcomeText('sent', 'b@x.org')).toMatch(/^Emailed the invite to b@x.org/)
    for (const status of ['failed', 'not_configured', null, undefined] as const) {
      expect(emailOutcomeText(status, 'b@x.org')).toMatch(/yourself/)
    }
    expect(emailOutcomeText('throttled', 'b@x.org')).toMatch(/less than a minute ago/)
  })
})
