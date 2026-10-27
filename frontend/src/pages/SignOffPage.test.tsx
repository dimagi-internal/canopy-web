// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { SignOffOut } from '@/api/signOffs'

const getSignOff = vi.fn<(id: string) => Promise<SignOffOut>>()
const decideSignOff = vi.fn()

vi.mock('@/api/signOffs', () => ({ getSignOff, decideSignOff }))

const { SignOffPage } = await import('./SignOffPage')

const ROW: SignOffOut = {
  id: 'so_1',
  site: 'connect-labs',
  workspace: 'connect',
  subject: 'ace@dimagi-ai.com',
  action_key: 'labs.mcp_token.full',
  action_title: 'Create a full-access labs MCP token',
  action_detail: '',
  approvers: 'agent_admins',
  agent_slug: 'ace',
  agent_name: 'ACE',
  state: 'open',
  created_at: '2026-10-27T12:00:00Z',
  expires_at: '2026-10-28T12:00:00Z',
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/sign-offs/so_1']}>
      <Routes>
        <Route path="/w/:workspace/sign-offs/:signOffId" element={<SignOffPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('SignOffPage', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('names the site, the action and the agent it is for', async () => {
    getSignOff.mockResolvedValue(ROW)
    renderPage()
    expect((await screen.findByTestId('sign-off-title')).textContent).toContain('Create a full-access labs MCP token')
    expect((screen.getByTestId('sign-off-for')).textContent).toContain('ACE, an agent you own or administer')
  })

  it('approving sends the browser back to the site', async () => {
    getSignOff.mockResolvedValue(ROW)
    decideSignOff.mockResolvedValue({ ...ROW, state: 'approved', redirect_url: 'https://labs.example.org/back?sign_off=x' })
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { value: { ...window.location, assign }, writable: true })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://labs.example.org/back?sign_off=x'))
    expect(decideSignOff).toHaveBeenCalledWith('so_1', true)
  })

  it('an expired request offers no buttons', async () => {
    getSignOff.mockResolvedValue({ ...ROW, state: 'expired' })
    renderPage()
    expect((await screen.findByTestId('sign-off-state')).textContent).toContain('expired')
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })
})
