// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const approve = vi.fn()
const decline = vi.fn()
vi.mock('@/api/actAs', async (orig) => ({
  ...(await orig<typeof import('@/api/actAs')>()),
  getActAsRequest: (id: string) => get(id),
  approveActAsRequest: (id: string, scope: string) => approve(id, scope),
  declineActAsRequest: (id: string) => decline(id),
  stopActAsRequest: vi.fn(),
}))

import { ActAsApprovalPage } from './ActAsApprovalPage'
import { offeredScopes } from '@/api/actAs'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const REQ = {
  id: 'r1', status: 'pending', scope: '', requested_scope: 'either', agent: 'hal', agent_name: 'Hal',
  person: 'jj@dimagi.com', reason: 'Move my three stale tasks to Done', turn_id: 'abcdef1234',
  session_url: '/w/dimagi/sessions/s1', expires_at: '2026-10-28T12:10:00Z',
}

function open() {
  return render(
    <MemoryRouter initialEntries={['/act-as/r1']}>
      <Routes>
        <Route path="/act-as/:requestId" element={<ActAsApprovalPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ActAsApprovalPage', () => {
  it('offers what was asked, read-only first', () => {
    expect(offeredScopes('either')).toEqual(['read', 'write'])
    expect(offeredScopes('read')).toEqual(['read'])
    expect(offeredScopes('write')).toEqual(['write'])
  })

  it('shows the agent, its reason verbatim and the conversation, and approves read-only', async () => {
    get.mockResolvedValue(REQ)
    approve.mockResolvedValue({ ...REQ, status: 'approved', scope: 'read' })
    open()
    expect(await screen.findByText('Move my three stale tasks to Done')).toBeTruthy()
    expect(screen.getByText('its conversation').getAttribute('href')).toBe('/w/dimagi/sessions/s1')
    const buttons = screen.getAllByRole('button').map((b) => b.textContent)
    expect(buttons).toEqual(['Allow read-only (30 min)', 'Allow read + write (30 min)', 'Decline'])
    fireEvent.click(screen.getByTestId('act-as-allow-read'))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('r1', 'read'))
    expect(await screen.findByText(/Read-only until/)).toBeTruthy()
  })

  it('a read request offers only read-only, and can be declined', async () => {
    get.mockResolvedValue({ ...REQ, requested_scope: 'read' })
    decline.mockResolvedValue({ ...REQ, status: 'declined' })
    open()
    await screen.findByText('Move my three stale tasks to Done')
    expect(screen.queryByTestId('act-as-allow-write')).toBeNull()
    fireEvent.click(screen.getByTestId('act-as-decline'))
    await waitFor(() => expect(decline).toHaveBeenCalledWith('r1'))
    expect(await screen.findByText('Declined.')).toBeTruthy()
  })
})
