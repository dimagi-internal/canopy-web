// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ItemAge } from './ItemAge'

afterEach(cleanup)

// Regression origin (2026-08-12): the inbox rendered a queue of undecided cards with
// no date anywhere on them. Jonathan: "I can't tell if these are recent or just old
// and I should close." Every case below is a card he could be looking at.
//
// Plain DOM assertions on purpose — this repo does not register @testing-library/jest-dom,
// so toHaveTextContent and friends are not available here.

const NOW = new Date('2026-08-12T18:00:00Z')

const text = () => screen.getByTestId('item-age').textContent ?? ''

describe('ItemAge', () => {
  it('shows how old a card is, which is the question being asked', () => {
    render(<ItemAge createdAt="2026-08-10T18:00:00Z" now={NOW} />)
    expect(text()).toContain('2d ago')
  })

  it('reads "just now" for a card posted this minute', () => {
    render(<ItemAge createdAt="2026-08-12T17:59:30Z" now={NOW} />)
    expect(text()).toContain('just now')
  })

  it('carries the absolute timestamp on hover, for when the exact day matters', () => {
    render(<ItemAge createdAt="2026-08-10T18:00:00Z" now={NOW} />)
    const title = screen.getByTestId('item-age').getAttribute('title') ?? ''
    expect(title).toMatch(/2026/)
    expect(title).toMatch(/Aug/)
  })

  it('adds the decision age on a card that has been decided', () => {
    render(<ItemAge createdAt="2026-08-10T18:00:00Z" decidedAt="2026-08-12T15:00:00Z" now={NOW} />)
    expect(text()).toContain('2d ago')
    expect(text()).toContain('decided 3h ago')
  })

  it('shows only the created age when the item is still open', () => {
    render(<ItemAge createdAt="2026-08-10T18:00:00Z" decidedAt={null} now={NOW} />)
    expect(text()).not.toContain('decided')
  })

  it('renders nothing rather than "NaNd ago" when the date is unusable', () => {
    const empty = render(<ItemAge createdAt="" now={NOW} />)
    expect(empty.container.innerHTML).toBe('')
    cleanup()
    const bad = render(<ItemAge createdAt="not-a-date" now={NOW} />)
    expect(bad.container.innerHTML).toBe('')
  })

  it('drops an unusable decided_at without losing the created age', () => {
    render(<ItemAge createdAt="2026-08-10T18:00:00Z" decidedAt="not-a-date" now={NOW} />)
    expect(text()).toContain('2d ago')
    expect(text()).not.toContain('decided')
  })
})
