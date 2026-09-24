// @vitest-environment jsdom
//
// An agent acts on GitHub as its OWNER. The owner gets the pre-filled GitHub
// form and the paste box; anyone else is told whose it is. A refused token
// shows the server's own reason — that sentence (wrong resource owner, repo not
// selected) is the whole diagnosis.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { AuthContext } from '@/auth/AuthProvider'

const { getAgentGitHub, setAgentGitHub, checkAgentGitHub, deleteAgentGitHub } = vi.hoisted(() => ({
  getAgentGitHub: vi.fn(),
  setAgentGitHub: vi.fn(),
  checkAgentGitHub: vi.fn(),
  deleteAgentGitHub: vi.fn(),
}))
vi.mock('@/api/agents', () => ({ getAgentGitHub, setAgentGitHub, checkAgentGitHub, deleteAgentGitHub }))

const { AgentGitHubSection } = await import('./AgentGitHubSection')

const UNSET = {
  repo: 'dimagi-internal/echo',
  owner_email: 'olive@dimagi.com',
  create_url: 'https://github.com/settings/personal-access-tokens/new?name=canopy+echo',
  set: false, login: '', name: '', expired: false, expiring_soon: false, checks: [], error: '',
}
const WORKING = {
  ...UNSET, set: true, login: 'olive', name: 'Olive', expires_at: '2026-12-01T10:00:00Z',
  checks: [{ repo: 'dimagi-internal/echo', ok: true, detail: 'can open pull requests' }],
}

function show(email: string) {
  const user = { email } as never
  return render(
    <AuthContext.Provider value={{ status: 'authenticated', user } as never}>
      <AgentGitHubSection slug="echo" />
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentGitHubSection', () => {
  it('gives the owner the pre-filled GitHub form and a paste box', async () => {
    getAgentGitHub.mockResolvedValue(UNSET)
    show('olive@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-setup')).toBeTruthy())
    expect(screen.getByRole('link', { name: 'Create the token on GitHub' }).getAttribute('href'))
      .toBe(UNSET.create_url)
    expect(screen.getByTestId('github-setup').textContent).toContain('dimagi-internal/echo')
    expect((screen.getByLabelText('GitHub token') as HTMLInputElement).type).toBe('password')
  })

  it('tells anyone else it is the owner’s to lend', async () => {
    getAgentGitHub.mockResolvedValue(UNSET)
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('agent-github')).toBeTruthy())
    expect(screen.queryByTestId('github-setup')).toBeNull()
    expect(screen.getByTestId('agent-github').textContent).toContain('Only the agent’s owner')
  })

  it('shows the server’s reason when a pasted token is refused', async () => {
    getAgentGitHub.mockResolvedValue(UNSET)
    setAgentGitHub.mockRejectedValue(new Error('dimagi-internal/echo: the token cannot see this repo'))
    show('olive@dimagi.com')
    await waitFor(() => expect(screen.getByLabelText('GitHub token')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('GitHub token'), { target: { value: 'github_pat_x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('cannot see this repo'))
  })

  it('says who it acts as and what it can do once set', async () => {
    getAgentGitHub.mockResolvedValue(WORKING)
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-status').textContent).toContain('@olive'))
    expect(screen.getByTestId('github-status').textContent).toContain('Working')
    expect(screen.getByTestId('github-checks').textContent).toContain('can open pull requests')
  })
})
