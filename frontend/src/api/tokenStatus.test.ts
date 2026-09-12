import { describe, it, expect } from 'vitest'
import { tokenStatus } from './tokenStatus'

describe('tokenStatus', () => {
  it('reports a live token with an expiry', () => {
    expect(tokenStatus({ revoked_at: null, expires_at: '2027-03-01T00:00:00Z' })).toBe(
      'expires 2027-03-01',
    )
  })

  it('reports a never-expiring token as active', () => {
    expect(tokenStatus({ revoked_at: null, expires_at: null })).toBe('active')
  })

  it('prefers revoked over expiry — a revoked token is dead regardless of its expiry', () => {
    // This is the case that must never regress: reporting "expires 2027-03-01"
    // for a revoked credential would tell someone it still works.
    expect(tokenStatus({ revoked_at: '2026-09-01T00:00:00Z', expires_at: '2027-03-01T00:00:00Z' }))
      .toBe('revoked')
  })
})
