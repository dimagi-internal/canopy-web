// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { USER_ROLES, rolesHeading } from '@/guide/paths'
import { AboutPage } from './AboutPage'

vi.mock('@/api/publicStats', () => ({ getPublicStats: vi.fn(async () => null) }))

afterEach(cleanup)

describe('AboutPage roles', () => {
  it('has a heading whose count equals the number of roles rendered', () => {
    render(<AboutPage />)
    const heading = screen.getByRole('heading', { level: 2, name: /roles?$/ })
    const section = heading.closest('section')!
    const cards = section.querySelectorAll('h3')
    expect(cards).toHaveLength(USER_ROLES.length)
    expect(heading.textContent).toBe(rolesHeading())
    const words = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']
    expect(heading.textContent!.toLowerCase().startsWith(words[cards.length - 1])).toBe(true)
  })
})
