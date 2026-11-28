// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const getMyLearningMode = vi.fn()
const setMyLearningMode = vi.fn()
vi.mock('@/api/learningMode', () => ({ getMyLearningMode, setMyLearningMode }))

const { LearningModeCard } = await import('./LearningModeCard')

const state = (on: boolean) => ({
  on,
  set_at: on ? '2026-11-20T10:00:00Z' : null,
  set_by: on ? { name: 'Pat Parent', email: 'pat@example.org', is_you: false } : null,
  workspaces: on ? ['home'] : [],
  readers: on
    ? {
        coaches: [{ kind: 'agent', slug: 'trellis', name: 'Trellis', workspace: 'home' }],
        workspace_admins: [{ workspace: 'home', people: [{ name: 'Pat Parent', email: 'pat@example.org' }] }],
        agent_admins: [{ agent: 'tutor', name: 'Tutor', people: [{ name: 'Ann Admin', email: 'ann@example.org' }] }],
      }
    : { coaches: [], workspace_admins: [], agent_admins: [] },
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('LearningModeCard', () => {
  it('says who turned it on and exactly who reads', async () => {
    getMyLearningMode.mockResolvedValue(state(true))
    render(<LearningModeCard />)
    expect((await screen.findByTestId('learning-mode-set-by')).textContent).toContain('by Pat Parent')
    expect(screen.getByText(/Trellis \(coach, agent, home\)/)).toBeTruthy()
    expect(screen.getByText(/Owners and admins of home: Pat Parent/)).toBeTruthy()
    expect(screen.getByText(/Admins of Tutor \(your sessions with it\): Ann Admin/)).toBeTruthy()
  })

  it('turns it off', async () => {
    getMyLearningMode.mockResolvedValue(state(true))
    setMyLearningMode.mockResolvedValue(state(false))
    render(<LearningModeCard />)
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Learning mode' }))
    await waitFor(() => expect(setMyLearningMode).toHaveBeenCalledWith(false))
    expect(await screen.findByText('Off')).toBeTruthy()
  })
})
