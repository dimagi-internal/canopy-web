// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { AgentOut } from '@/api/agents'
import { AgentKpiCard } from './AgentKpiCard'

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
    turn_mode: 'gated',
    created_at: '2026-07-31T00:00:00Z',
    updated_at: '2026-07-31T00:00:00Z',
    ...overrides,
  } as AgentOut
}

const renderCard = (a: AgentOut, waiting = 0) =>
  render(
    <MemoryRouter>
      <AgentKpiCard agent={a} waiting={waiting} />
    </MemoryRouter>,
  )

afterEach(cleanup)

describe('AgentKpiCard', () => {
  it('badges an agent running in auto mode', () => {
    renderCard(agent({ turn_mode: 'auto' }))
    expect(screen.getByTestId('agent-auto-echo').textContent).toBe('Auto')
  })

  it('shows no badge for a gated agent — the default posture stays quiet', () => {
    renderCard(agent({ turn_mode: 'gated' }))
    expect(screen.queryByTestId('agent-auto-echo')).toBeNull()
  })

  it('still renders the waiting count alongside the badge', () => {
    renderCard(agent({ turn_mode: 'auto' }), 3)
    expect(screen.getByTestId('agent-auto-echo')).toBeTruthy()
    expect(screen.getByText('3 waiting on you')).toBeTruthy()
  })
})
