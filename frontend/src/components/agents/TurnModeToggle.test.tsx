// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { TurnMode } from '@/api/agents'

const setAgentTurnMode = vi.fn<(slug: string, mode: TurnMode) => Promise<TurnMode>>()

vi.mock('@/api/agents', () => ({ setAgentTurnMode }))

const { TurnModeToggle } = await import('./TurnModeToggle')

const checked = (testId: string) =>
  screen.getByTestId(testId).getAttribute('aria-checked')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TurnModeToggle', () => {
  it('renders the initial mode as checked', () => {
    render(<TurnModeToggle agentSlug="echo" initialMode="manual" />)
    expect(checked('turn-mode-manual')).toBe('true')
    expect(checked('turn-mode-auto')).toBe('false')
  })

  it('flips the mode through the API on click', async () => {
    setAgentTurnMode.mockResolvedValue('auto')
    render(<TurnModeToggle agentSlug="echo" initialMode="manual" />)
    fireEvent.click(screen.getByTestId('turn-mode-auto'))
    await waitFor(() => expect(setAgentTurnMode).toHaveBeenCalledWith('echo', 'auto'))
    expect(checked('turn-mode-auto')).toBe('true')
  })

  it('reverts and shows the error when the API rejects', async () => {
    setAgentTurnMode.mockRejectedValue(new Error('nope'))
    render(<TurnModeToggle agentSlug="echo" initialMode="manual" />)
    fireEvent.click(screen.getByTestId('turn-mode-auto'))
    await waitFor(() => expect(screen.getByText(/nope/)).toBeTruthy())
    expect(checked('turn-mode-manual')).toBe('true')
  })

  it('clicking the current mode is a no-op', () => {
    render(<TurnModeToggle agentSlug="echo" initialMode="auto" />)
    fireEvent.click(screen.getByTestId('turn-mode-auto'))
    expect(setAgentTurnMode).not.toHaveBeenCalled()
  })
})
