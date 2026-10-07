// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { TurnModeBadge } from './TurnModeBadge'

afterEach(cleanup)

describe('TurnModeBadge', () => {
  it.each(['manual', 'auto'])('labels a %s session', (mode) => {
    render(<TurnModeBadge mode={mode} testId="m" />)
    expect(screen.getByTestId('m').textContent).toBe(mode)
  })

  it('renders nothing before a turn has been claimed', () => {
    const { container } = render(<TurnModeBadge mode="" testId="m" />)
    expect(container.innerHTML).toBe('')
  })
})
