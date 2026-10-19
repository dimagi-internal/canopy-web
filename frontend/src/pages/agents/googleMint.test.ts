import { describe, expect, it } from 'vitest'
import { declaresMailbox, mintOutcome } from './googleMint'

describe('declaresMailbox', () => {
  it('offers the button only to an agent that declares a mailbox', () => {
    expect(declaresMailbox([{ name: 'gog-token' }, { name: 'nova-api-key' }])).toBe(true)
    expect(declaresMailbox([{ name: 'nova-api-key' }])).toBe(false)
  })

  it('offers it to an agent with a mailbox address even when it declares nothing', () => {
    // ada/echo/eva/hal: no runtime.yaml (declared: 0), but every one has a mailbox.
    expect(declaresMailbox([], 'ada@dimagi-ai.com')).toBe(true)
    expect(declaresMailbox([], '')).toBe(false)
    expect(declaresMailbox([], null)).toBe(false)
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

  it('names both accounts when Google signed in as the wrong one', () => {
    const m = mintOutcome('wrong-account', { account: 'jj@gmail.com', agentEmail: 'ace@dimagi-ai.com' })
    expect(m?.tone).toBe('error')
    expect(m?.text).toBe(
      'Google signed in as jj@gmail.com, but this agent is ace@dimagi-ai.com. Sign in to Google as the agent and try again.',
    )
  })

  it('sends a vault with no keys to setting up the vault first, with a link there', () => {
    const m = mintOutcome('zk-no-recipients')
    expect(m?.tone).toBe('error')
    expect(m?.text).toMatch(/vault has no keys yet .* set up its vault \(passkey\) first, then connect Google/)
    expect(m?.link).toEqual({ href: '/settings#vault-key', label: 'Set up your vault key' })
  })
})
