import { describe, expect, it } from 'vitest'
import { avatarFor } from './avatar'

describe('avatarFor', () => {
  it('takes initials from a two-part display name', () => {
    expect(avatarFor('alice@x.com', 'Alice Chen').initials).toBe('AC')
  })

  it('falls back to the email local-part when there is no name', () => {
    expect(avatarFor('bob.ali@x.com', '').initials).toBe('BA')
  })

  it('produces a single initial for a one-word identity', () => {
    expect(avatarFor('ace@x.com', 'ACE').initials).toBe('A')
  })

  it('is deterministic: the same email is always the same color', () => {
    expect(avatarFor('alice@x.com', 'Alice Chen').colorClass)
      .toBe(avatarFor('alice@x.com', 'Different Name').colorClass)
  })

  it('keys color on email, not name, so a rename does not recolor someone', () => {
    const a = avatarFor('alice@x.com', 'Alice Chen')
    const b = avatarFor('zoe@y.com', 'Alice Chen')
    expect(a.colorClass).not.toBe(b.colorClass)
  })
})
