// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SessionAudienceLine } from './SessionAudienceLine'

describe('SessionAudienceLine', () => {
  afterEach(() => cleanup())

  it('says who can see it, then who else may', () => {
    render(<SessionAudienceLine audience={{
      label: 'Only you and 1 person you added', extras: ['Trellis may review (coaching)'], count: 3,
    }} />)
    const line = screen.getByTestId('session-audience')
    expect(line.textContent).toBe(
      'Who can see this: Only you and 1 person you added · Trellis may review (coaching)')
    expect(line.getAttribute('title')).toBe('3 people can read this')
  })

  it('renders nothing without an audience', () => {
    render(<SessionAudienceLine audience={null} />)
    expect(screen.queryByTestId('session-audience')).toBeNull()
  })
})
