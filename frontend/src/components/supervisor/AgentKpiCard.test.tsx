// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { AgentOut, TurnMode } from '@/api/agents'

const setAgentTurnMode = vi.fn<(slug: string, mode: TurnMode) => Promise<TurnMode>>()
vi.mock('@/api/agents', () => ({ setAgentTurnMode }))

const { AgentKpiCard } = await import('./AgentKpiCard')

function agent(overrides: Partial<AgentOut> = {}): AgentOut {
  return {
    id: 1,
    slug: 'echo',
    name: 'Echo',
    description: '',
    persona: '',
    email: '',
    avatar_url: '',
    workspace: 'canopy',
    runner_preference: [],
    turn_mode: 'manual',
    created_at: '2026-07-31T00:00:00Z',
    updated_at: '2026-07-31T00:00:00Z',
    ...overrides,
  } as AgentOut
}

// Renders the card inside a router that also has a destination route, so a
// stray navigation caused by the chip tap is observable rather than silent.
const renderCard = (a: AgentOut, waiting = 0) =>
  render(
    <MemoryRouter initialEntries={['/supervisor']}>
      <Routes>
        <Route path="/supervisor" element={<AgentKpiCard agent={a} waiting={waiting} />} />
        <Route path="/w/canopy/agents/echo" element={<div data-testid="agent-page" />} />
      </Routes>
    </MemoryRouter>,
  )

beforeEach(() => {
  setAgentTurnMode.mockResolvedValue('auto')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('AgentKpiCard turn-mode chip', () => {
  it('reads out the current mode on each card', () => {
    renderCard(agent({ turn_mode: 'auto' }))
    expect(screen.getByTestId('agent-mode-echo').textContent).toBe('Auto')
    cleanup()
    renderCard(agent({ turn_mode: 'manual' }))
    expect(screen.getByTestId('agent-mode-echo').textContent).toBe('Manual')
  })

  it('turning auto ON asks first, then flips', async () => {
    renderCard(agent({ turn_mode: 'manual' }))
    fireEvent.click(screen.getByTestId('agent-mode-echo'))
    expect(window.confirm).toHaveBeenCalledOnce()
    await waitFor(() => expect(setAgentTurnMode).toHaveBeenCalledWith('echo', 'auto'))
    expect(screen.getByTestId('agent-mode-echo').textContent).toBe('Auto')
  })

  it('declining the confirm leaves the agent manual and calls nothing', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderCard(agent({ turn_mode: 'manual' }))
    fireEvent.click(screen.getByTestId('agent-mode-echo'))
    expect(setAgentTurnMode).not.toHaveBeenCalled()
    expect(screen.getByTestId('agent-mode-echo').textContent).toBe('Manual')
  })

  it('turning auto OFF needs no confirm — the safe direction is one tap', async () => {
    renderCard(agent({ turn_mode: 'auto' }))
    fireEvent.click(screen.getByTestId('agent-mode-echo'))
    expect(window.confirm).not.toHaveBeenCalled()
    await waitFor(() => expect(setAgentTurnMode).toHaveBeenCalledWith('echo', 'manual'))
  })

  it('tapping the chip does not navigate to the agent page', async () => {
    renderCard(agent({ turn_mode: 'auto' }))
    fireEvent.click(screen.getByTestId('agent-mode-echo'))
    await waitFor(() => expect(setAgentTurnMode).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-page')).toBeNull()
    expect(screen.getByTestId('agent-card-echo')).toBeTruthy()
  })

  it('reverts and offers a retry when the write fails', async () => {
    setAgentTurnMode.mockRejectedValue(new Error('offline'))
    renderCard(agent({ turn_mode: 'auto' }))
    fireEvent.click(screen.getByTestId('agent-mode-echo'))
    await waitFor(() => expect(screen.getByTestId('agent-mode-echo').textContent).toBe('Retry'))
  })

  it('still renders the waiting count alongside the chip', () => {
    renderCard(agent({ turn_mode: 'auto' }), 3)
    expect(screen.getByTestId('agent-mode-echo')).toBeTruthy()
    expect(screen.getByText('3 waiting on you')).toBeTruthy()
  })
})
