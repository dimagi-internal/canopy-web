import { describe, expect, it } from 'vitest'
import { declaresMailbox, mintOutcome } from './googleMint'

describe('declaresMailbox', () => {
  it('offers the button only to an agent that declares a mailbox', () => {
    expect(declaresMailbox([{ name: 'gog-token' }, { name: 'nova-api-key' }])).toBe(true)
    expect(declaresMailbox([{ name: 'nova-api-key' }])).toBe(false)
  })
})

describe('mintOutcome', () => {
  it('says nothing when there is nothing to say', () => {
    expect(mintOutcome(null)).toBeNull()
    expect(mintOutcome('something-else')).toBeNull()
  })

  it('reports success', () => {
    expect(mintOutcome('ok')?.tone).toBe('ok')
  })

  it('gives the missing-refresh-token case its own remedy', () => {
    // The only outcome that would otherwise read as success: a token with no
    // refresh token imports cleanly and then can never refresh. A generic "it
    // failed" would send the operator looking in the wrong place.
    const m = mintOutcome('no-refresh-token')
    expect(m?.tone).toBe('error')
    expect(m?.text).toMatch(/already granted/i)
  })

  it('distinguishes a cancelled sign-in from a real failure', () => {
    expect(mintOutcome('denied')?.text).toMatch(/cancelled/i)
    expect(mintOutcome('exchange-failed')?.text).toMatch(/failed/i)
  })
})
