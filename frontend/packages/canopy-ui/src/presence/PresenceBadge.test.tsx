// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PresenceBadge } from './PresenceBadge'
import type { Viewer } from './usePresence'

// NOTE ON CONVENTION: canopy-web has no @testing-library/jest-dom and no
// user-event package. Assertions use toBeTruthy(), interactions use
// fireEvent, and every DOM test carries the `@vitest-environment jsdom`
// docblock above — the vitest config sets no global environment. Do not
// introduce toBeInTheDocument() here; it will not exist.

const viewer = (n: number, over: Partial<Viewer> = {}): Viewer => ({
  email: `u${n}@x.com`,
  name: `User ${n}`,
  subLocation: 'run overview',
  idle: false,
  self: false,
  ...over,
})

describe('PresenceBadge', () => {
  afterEach(cleanup)

  it('renders nothing when you are the only viewer', () => {
    const { container } = render(<PresenceBadge viewers={[viewer(1, { self: true })]} />)
    expect(container.innerHTML).toBe('')
  })

  it('renders nothing when the roster is empty', () => {
    const { container } = render(<PresenceBadge viewers={[]} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows at most three avatars and collapses the rest into +N', () => {
    render(<PresenceBadge viewers={[viewer(1), viewer(2), viewer(3), viewer(4), viewer(5)]} />)
    expect(screen.getByText('+2')).toBeTruthy()
  })

  it('labels the control with the viewer count for screen readers', () => {
    render(<PresenceBadge viewers={[viewer(1), viewer(2)]} />)
    expect(screen.getByRole('button', { name: /2 people viewing this page/i })).toBeTruthy()
  })

  it('expands to a named list on click, listing you first and marked', () => {
    render(<PresenceBadge viewers={[viewer(1), viewer(2, { self: true, name: 'Me' })]} />)
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByText(/Me/)).toBeTruthy()
    expect(screen.getByText('(you)')).toBeTruthy()
    expect(screen.getByText('User 1')).toBeTruthy()
  })

  it('renders two viewers with no email without colliding their React keys', () => {
    // Email is not guaranteed non-empty (the server sends "" for a user with
    // none), so it cannot be the row key. A collision here shows up as a
    // React duplicate-key error, not as a visibly missing row.
    const errors: unknown[][] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args) => {
      errors.push(args)
    })
    try {
      render(
        <PresenceBadge
          viewers={[
            viewer(1, { email: '', name: 'Ana' }),
            viewer(2, { email: '', name: 'Bo' }),
          ]}
        />,
      )
      fireEvent.click(screen.getByRole('button'))
      expect(screen.getByText('Ana')).toBeTruthy()
      expect(screen.getByText('Bo')).toBeTruthy()
    } finally {
      spy.mockRestore()
    }
    expect(errors.flat().join(' ')).not.toMatch(/same key/i)
  })

  it('marks idle viewers in the expanded list', () => {
    render(<PresenceBadge viewers={[viewer(1, { idle: true }), viewer(2)]} />)
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByText('idle')).toBeTruthy()
  })
})
