// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getMySessionLearning = vi.fn()
vi.mock('@/api/learning', () => ({
  getMySessionLearning,
  removeMySession: vi.fn(),
  restoreMySession: vi.fn(),
  declareMySessionProject: vi.fn(),
  undeclareMySessionProject: vi.fn(),
}))
vi.mock('@/api/work', () => ({ listProjects: vi.fn().mockResolvedValue([]) }))

const { ChatSessionMenu } = await import('./ChatSessionMenu')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const props = {
  sessionId: 's1',
  myRole: 'participant' as string | null,
  onToggleNotify: vi.fn(),
  onShare: vi.fn(async () => {}),
  onReset: vi.fn(),
  resetting: false,
  onClose: vi.fn(),
  closing: false,
}

async function openMenu() {
  fireEvent.click(screen.getByTestId('chat-session-menu'))
  await waitFor(() => expect(screen.getByText('People…')).toBeTruthy())
}

describe('ChatSessionMenu — Learning…', () => {
  it('shows a participant the item, marked when removed, and opens the control', async () => {
    getMySessionLearning.mockResolvedValue({ session_id: 's1', excluded: true, projects: [] })
    render(<MemoryRouter><ChatSessionMenu {...props} /></MemoryRouter>)
    await waitFor(() => expect(getMySessionLearning).toHaveBeenCalledWith('s1'))
    await openMenu()
    const item = await screen.findByTestId('session-learning')
    expect(item.textContent).toContain('removed')
    fireEvent.click(item)
    expect(await screen.findByText('Let learning use this again')).toBeTruthy()
  })

  it('hides it from anyone the route refuses (not in the session, or not a person)', async () => {
    getMySessionLearning.mockRejectedValue(new Error('Session not found'))
    render(<MemoryRouter><ChatSessionMenu {...props} /></MemoryRouter>)
    await waitFor(() => expect(getMySessionLearning).toHaveBeenCalled())
    await openMenu()
    expect(screen.queryByTestId('session-learning')).toBeNull()
  })
})
