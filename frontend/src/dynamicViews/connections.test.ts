import { describe, expect, it } from 'vitest'
import { checkConnect, connectOutcome, lendStartPath } from './connections'

describe('checkConnect', () => {
  it('refuses an agent the view does not declare', () => {
    expect(checkConnect({ agent: 'eva', provider: 'google' }, ['bailey'])).toMatch(/declares no task/)
  })
  it('refuses a time limit out of range rather than dropping it', () => {
    expect(checkConnect({ agent: 'bailey', provider: 'google', minutes: 1 }, ['bailey'])).toMatch(/5 to 10080/)
    expect(checkConnect({ agent: 'bailey', provider: 'google', minutes: 20000 }, ['bailey'])).toMatch(/5 to 10080/)
  })
  it('keeps the limits it was given', () => {
    expect(checkConnect({ agent: 'bailey', provider: 'google', minutes: 240, ownerTurnsOnly: true }, ['bailey']))
      .toEqual({ agent: 'bailey', provider: 'google', minutes: 240, ownerTurnsOnly: true })
  })
})

describe('lendStartPath', () => {
  it('starts the connect with the limits, coming back to the page', () => {
    const url = lendStartPath({ agent: 'bailey', provider: 'google', minutes: 240, ownerTurnsOnly: true }, 'marshellis', '/w/marshellis/views/2')
    const q = new URL(url, 'https://x.invalid').searchParams
    expect(url).toContain('/auth/connections/google/marshellis/start/')
    expect(Object.fromEntries(q)).toEqual({
      return_to: '/w/marshellis/views/2', for_agent: 'bailey', lend_minutes: '240', owner_turns_only: '1',
    })
  })
})

describe('connectOutcome', () => {
  it('reads what the server added, and nothing when it added nothing', () => {
    expect(connectOutcome('?connection=google&status=error&reason=no_vault_key'))
      .toEqual({ provider: 'google', status: 'error', reason: 'no_vault_key' })
    expect(connectOutcome('?tab=x')).toBeNull()
  })
})
