// @vitest-environment jsdom
//
// An agent acts on GitHub as whoever delegated it a token: its owner or an admin.
// Anyone the server says may delegate (`can_lend`) gets the
// pre-filled GitHub form and the paste box; anyone else is told whose it is. A refused token
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
  lent_by_email: '',
  can_lend: true,
  create_url: 'https://github.com/settings/personal-access-tokens/new?name=canopy+echo',
  set: false, login: '', name: '', expired: false, expiring_soon: false, checks: [], error: '',
}
const WORKING = {
  ...UNSET, set: true, lent_by_email: 'olive@dimagi.com', login: 'olive', name: 'Olive', expires_at: '2026-12-01T10:00:00Z',
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

  it('gives an admin the paste box too, and says whose token is in force', async () => {
    getAgentGitHub.mockResolvedValue({ ...WORKING, lent_by_email: 'ada@dimagi-ai.com', login: 'ada' })
    show('ada@dimagi-ai.com')
    await waitFor(() => expect(screen.getByTestId('github-setup')).toBeTruthy())
    expect(screen.getByTestId('github-status').textContent).toContain('delegated by ada@dimagi-ai.com')
    expect(screen.getByRole('button', { name: 'Withdraw' })).toBeTruthy()
  })

  it('offers Withdraw only to the person whose token is in force', async () => {
    getAgentGitHub.mockResolvedValue(WORKING)
    show('ada@dimagi-ai.com')
    await waitFor(() => expect(screen.getByTestId('github-setup')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Withdraw' })).toBeNull()
  })

  it('says which machines a token given now is sealed to, before it is pasted', async () => {
    getAgentGitHub.mockResolvedValue({ ...UNSET, eligible_machines: ['cloud-ec2-2', 'jj-mbp-cdp'] })
    show('olive@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-who-can-open')).toBeTruthy())
    const text = screen.getByTestId('github-who-can-open').textContent
    expect(text).toContain('cloud-ec2-2, jj-mbp-cdp')
    expect(text).toContain('Nobody can read it in canopy, including you')
  })

  it('says a delegated token in the vault can be opened only by its machines, and is not re-checked here', async () => {
    getAgentGitHub.mockResolvedValue({ ...WORKING, in_vault: true, machines: ['cloud-ec2-2'],
                                       checked_by: 'cloud-ec2-2', checked_at: '2026-10-26T10:00:00Z' })
    show('olive@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-machines')).toBeTruthy())
    expect(screen.getByTestId('github-machines').textContent).toContain('Only these machines can open it: cloud-ec2-2')
    expect(screen.getByTestId('github-status').textContent).toContain('by cloud-ec2-2')
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull()
  })

  it('tells its person to replace a token a machine found no longer works', async () => {
    getAgentGitHub.mockResolvedValue({ ...WORKING, in_vault: true, no_longer_works: true })
    show('olive@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-status').textContent).toContain('Not working'))
    expect(screen.getByTestId('agent-github').textContent).toContain('no longer works — olive@dimagi.com replaces it')
  })

  it('tells anyone else it is the owner’s or an admin’s to delegate', async () => {
    getAgentGitHub.mockResolvedValue({ ...UNSET, can_lend: false })
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('agent-github')).toBeTruthy())
    expect(screen.queryByTestId('github-setup')).toBeNull()
    expect(screen.getByTestId('agent-github').textContent).toContain('or one of its')
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

  it('lists the repos whose agentless turns borrow this identity, read-only', async () => {
    getAgentGitHub.mockResolvedValue({ ...WORKING, identity_for_repos: ['canopy-web', 'connect-labs'] })
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-identity-for-repos')).toBeTruthy())
    const text = screen.getByTestId('github-identity-for-repos').textContent
    expect(text).toContain('canopy-web')
    expect(text).toContain('connect-labs')
  })

  it('says nothing about repos when none borrow it', async () => {
    getAgentGitHub.mockResolvedValue(WORKING)
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-status')).toBeTruthy())
    expect(screen.queryByTestId('github-identity-for-repos')).toBeNull()
  })

  it('says who it acts as and what it can do once set', async () => {
    getAgentGitHub.mockResolvedValue(WORKING)
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('github-status').textContent).toContain('@olive'))
    expect(screen.getByTestId('github-status').textContent).toContain('Working')
    expect(screen.getByTestId('github-checks').textContent).toContain('can open pull requests')
  })
})
