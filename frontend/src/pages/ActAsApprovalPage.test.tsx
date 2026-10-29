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
import { offeredScopes, statusLine } from '@/api/actAs'

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
    expect(await screen.findByText(/Approved, read-only until/)).toBeTruthy()
  })

  it('a read request offers only read-only, and can be declined', async () => {
    get.mockResolvedValue({ ...REQ, requested_scope: 'read' })
    decline.mockResolvedValue({ ...REQ, status: 'declined' })
    open()
    await screen.findByText('Move my three stale tasks to Done')
    expect(screen.queryByTestId('act-as-allow-write')).toBeNull()
    fireEvent.click(screen.getByTestId('act-as-decline'))
    await waitFor(() => expect(decline).toHaveBeenCalledWith('r1'))
    expect(await screen.findByText('You declined this request.')).toBeTruthy()
  })

  it('says plainly what happened, with times', () => {
    const at = '2026-10-29T02:34:00Z'
    const t = new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    const now = new Date('2026-10-29T03:00:00Z').getTime()
    expect(statusLine({ ...REQ, status: 'expired', expires_at: at }, 'Ada', now)).toBe(
      `This request expired at ${t} — ask Ada again.`)
    expect(statusLine({ ...REQ, status: 'pending', expires_at: at }, 'Ada', now)).toBe(
      `Waiting for you — decide by ${t}.`)
    expect(statusLine({ ...REQ, status: 'approved', scope: 'read', expires_at: at, approved_at: at }, 'Ada', now))
      .toBe(`Approved, read-only until ${t}.`)
  })

  it('an expired request shows no buttons and no approval text', async () => {
    get.mockResolvedValue({ ...REQ, status: 'expired', expires_at: '2026-10-29T02:34:00Z' })
    open()
    expect(await screen.findByText(/This request expired at .* — ask Hal again\./)).toBeTruthy()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryByText(/Approving lets/)).toBeNull()
  })
})
