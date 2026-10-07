// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { TaskAge } from './TaskAge'

afterEach(cleanup)

// Regression origin (2026-08-12): the inbox rendered a queue of undecided cards with
// no date anywhere on them. Jonathan: "I can't tell if these are recent or just old
// and I should close." Every case below is a card he could be looking at.
//
// Plain DOM assertions on purpose — this repo does not register @testing-library/jest-dom,
// so toHaveTextContent and friends are not available here.

const NOW = new Date('2026-08-12T18:00:00Z')

const text = () => screen.getByTestId('task-age').textContent ?? ''

describe('TaskAge', () => {
  it('shows how old a card is, which is the question being asked', () => {
    render(<TaskAge createdAt="2026-08-10T18:00:00Z" now={NOW} />)
    expect(text()).toContain('2d ago')
  })

  it('reads "just now" for a card posted this minute', () => {
    render(<TaskAge createdAt="2026-08-12T17:59:30Z" now={NOW} />)
    expect(text()).toContain('just now')
  })

  it('carries the absolute timestamp on hover, for when the exact day matters', () => {
    render(<TaskAge createdAt="2026-08-10T18:00:00Z" now={NOW} />)
    const title = screen.getByTestId('task-age').getAttribute('title') ?? ''
    expect(title).toMatch(/2026/)
    expect(title).toMatch(/Aug/)
  })

  it('adds the closed age on a card whose ask has been closed', () => {
    render(<TaskAge createdAt="2026-08-10T18:00:00Z" closedAt="2026-08-12T15:00:00Z" now={NOW} />)
    expect(text()).toContain('2d ago')
    expect(text()).toContain('closed 3h ago')
  })

  it('shows only the created age when the ask is still open', () => {
    render(<TaskAge createdAt="2026-08-10T18:00:00Z" closedAt={null} now={NOW} />)
    expect(text()).not.toContain('closed')
  })

  it('renders nothing rather than "NaNd ago" when the date is unusable', () => {
    const empty = render(<TaskAge createdAt="" now={NOW} />)
    expect(empty.container.innerHTML).toBe('')
    cleanup()
    const bad = render(<TaskAge createdAt="not-a-date" now={NOW} />)
    expect(bad.container.innerHTML).toBe('')
  })

  it('drops an unusable ask_closed_at without losing the created age', () => {
    render(<TaskAge createdAt="2026-08-10T18:00:00Z" closedAt="not-a-date" now={NOW} />)
    expect(text()).toContain('2d ago')
    expect(text()).not.toContain('closed')
  })
})
