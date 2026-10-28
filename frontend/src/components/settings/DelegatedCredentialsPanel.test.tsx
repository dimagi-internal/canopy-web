// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const list = vi.fn()
const withdraw = vi.fn()
vi.mock('@/api/delegatedCredentials', () => ({
  listMyDelegatedCredentials: () => list(),
  withdrawMyDelegatedCredential: (id: number) => withdraw(id),
}))

import { DelegatedCredentialsPanel } from './DelegatedCredentialsPanel'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const ROW = {
  id: 3, agent: 'ace', agent_name: 'ACE', name: 'github-token', account: 'delegated', provider: 'github',
  label: 'GitHub · delegated by Jonathan Jackson · GitHub says it expires 2 Nov 2026',
  identity: { login: 'jjackson' }, machines: ['cloud-ec2-2', 'jj-mbp-cdp'], waiting: [], in_force: true,
  checks: [],
}

describe('DelegatedCredentialsPanel', () => {
  it('lists what you delegated, who can open it, and withdraws it', async () => {
    list.mockResolvedValueOnce([ROW]).mockResolvedValueOnce([])
    withdraw.mockResolvedValue(true)
    render(<DelegatedCredentialsPanel />)
    expect(await screen.findByText('ACE')).toBeTruthy()
    expect(screen.getByText('GitHub as @jjackson')).toBeTruthy()
    expect(screen.getByText('cloud-ec2-2, jj-mbp-cdp')).toBeTruthy()
    expect(screen.getByText('GitHub says it expires 2 Nov 2026')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Revoke at GitHub' }).getAttribute('href'))
      .toBe('https://github.com/settings/personal-access-tokens')
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw' }))
    await waitFor(() => expect(withdraw).toHaveBeenCalledWith(3))
    expect(await screen.findByText(/delegated no credentials/)).toBeTruthy()
  })

  it('shows a delegated Google account and where to revoke it', async () => {
    list.mockResolvedValueOnce([{
      ...ROW, id: 4, agent: 'bailey', agent_name: 'Bailey', name: 'google-delegated-gina@gmail.com',
      provider: 'google', label: 'Google · delegated by Gina', identity: { email: 'gina@gmail.com' },
      machines: ['cloud-ec2-2-marshellis'],
    }])
    render(<DelegatedCredentialsPanel />)
    expect(await screen.findByText('Google as gina@gmail.com')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Revoke at Google' }).getAttribute('href'))
      .toBe('https://myaccount.google.com/connections')
    expect(screen.getByRole('button', { name: 'Withdraw' })).toBeTruthy()
  })

  it('says when one is no longer used because you are no longer the agent’s admin', async () => {
    list.mockResolvedValueOnce([{ ...ROW, in_force: false }])
    render(<DelegatedCredentialsPanel />)
    expect(await screen.findByText(/no longer its admin/)).toBeTruthy()
  })
})
