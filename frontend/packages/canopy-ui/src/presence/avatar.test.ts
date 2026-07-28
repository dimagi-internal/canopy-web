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

  it('color is a deterministic function of email via djb2', () => {
    const email = 'alice@example.com'
    const COLORS = [
      'bg-sky-600',
      'bg-emerald-600',
      'bg-violet-600',
      'bg-amber-600',
      'bg-rose-600',
      'bg-teal-600',
      'bg-indigo-600',
      'bg-fuchsia-600',
    ]

    // Compute expected color using djb2 formula
    let h = 5381
    for (let i = 0; i < email.length; i++) h = ((h << 5) + h + email.charCodeAt(i)) | 0
    const expectedColorClass = COLORS[Math.abs(h) % COLORS.length]

    expect(avatarFor(email, 'Any Name').colorClass).toBe(expectedColorClass)
  })
})
