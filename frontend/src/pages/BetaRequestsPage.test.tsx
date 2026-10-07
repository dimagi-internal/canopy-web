// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { BetaRequestOut } from '@/api/betaRequests'

const getBetaRequest = vi.fn<(id: number) => Promise<BetaRequestOut>>()
const inviteBetaRequest = vi.fn<(id: number, ws: string, role: string) => Promise<BetaRequestOut>>()
const declineBetaRequest = vi.fn<(id: number) => Promise<BetaRequestOut>>()

class FakeWorkspaceApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/betaRequests', () => ({
  getBetaRequest, inviteBetaRequest, declineBetaRequest, listBetaRequests: vi.fn(),
}))
vi.mock('@/api/workspaces', () => ({ WorkspaceApiError: FakeWorkspaceApiError }))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    workspaces: [
      { slug: 'acme', display_name: 'Acme', role: 'viewer' },
      { slug: 'partners', display_name: 'Partners', role: 'owner' },
    ],
    active: 'acme',
    loading: false,
    refresh: vi.fn(),
  }),
}))

const { BetaRequestPage } = await import('./BetaRequestsPage')

function request(over: Partial<BetaRequestOut> = {}): BetaRequestOut {
  return {
    id: 3, email: 'mhayto@dimagi.com', reason: 'Test for SureAdhere!', created_at: '2026-10-07T10:00:00Z',
    status: 'pending', workspace: null, workspace_name: null, role: '', decided_by: null,
    decided_at: null, email_status: null, ...over,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/beta-requests/3']}>
      <Routes>
        <Route path="/beta-requests/:requestId" element={<BetaRequestPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BetaRequestPage', () => {
  it('offers only workspaces you can invite into, and invites there', async () => {
    getBetaRequest.mockResolvedValue(request())
    inviteBetaRequest.mockResolvedValue(request({
      status: 'invited', workspace: 'partners', workspace_name: 'Partners', role: 'editor', email_status: 'sent',
    }))
    renderPage()
    expect(await screen.findByText('Test for SureAdhere!')).toBeTruthy()

    const ws = screen.getByLabelText('Invite them to') as HTMLSelectElement
    expect(Array.from(ws.options).map((o) => o.value)).toEqual(['partners'])
    fireEvent.change(screen.getByLabelText('as'), { target: { value: 'editor' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve and invite' }))

    await waitFor(() => expect(inviteBetaRequest).toHaveBeenCalledWith(3, 'partners', 'editor'))
    expect(await screen.findByText(/Invited to Partners as editor/)).toBeTruthy()
    expect(screen.getByText(/emailed the invite link/)).toBeTruthy()
  })

  it('tells a non-reviewer whose requests these are', async () => {
    getBetaRequest.mockRejectedValue(new FakeWorkspaceApiError(404, 'not found'))
    renderPage()
    expect(await screen.findByText(/answered by the person they are emailed to/)).toBeTruthy()
  })
})
