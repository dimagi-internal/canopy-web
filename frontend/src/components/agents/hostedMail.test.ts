import { describe, expect, it } from 'vitest'
import { hostedDomainOf, mailboxHint } from './hostedMail'

describe('hosted mailbox hint', () => {
  const hosted = ['marshellis.com']
  it('says a hosted address needs no sign-in', () => {
    expect(hostedDomainOf('Scout@Marshellis.com', hosted)).toBe('marshellis.com')
    expect(mailboxHint('scout@marshellis.com', hosted)).toMatch(/Canopy-hosted email — nothing to sign in/)
  })
  it('keeps the Google sign-in hint for any other address', () => {
    expect(mailboxHint('scout@gmail.com', hosted)).toMatch(/sign it in with Google later/)
  })
  it('offers the hosted domain while the field is empty', () => {
    expect(mailboxHint('', hosted)).toMatch(/@marshellis.com is canopy-hosted/)
    expect(mailboxHint('', [])).toMatch(/sign it in with Google later/)
  })
})
