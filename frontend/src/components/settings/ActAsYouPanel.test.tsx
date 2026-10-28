// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const list = vi.fn()
const stop = vi.fn()
vi.mock('@/api/hostGrants', () => ({
  listMyHostGrants: () => list(),
  stopMyHostGrant: (id: number) => stop(id),
}))

const listCanopy = vi.fn()
const stopCanopy = vi.fn()
vi.mock('@/api/actAs', () => ({
  listMyActAsRequests: () => listCanopy(),
  stopActAsRequest: (id: string) => stopCanopy(id),
}))

import { MemoryRouter } from 'react-router-dom'
import { ActAsYouPanel } from './ActAsYouPanel'

function renderPanel() {
  return render(
    <MemoryRouter>
      <ActAsYouPanel />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const ROW = {
  id: 7, site: 'connect-labs', agent: 'ace', agent_name: 'ACE', scope: 'marketplace:read',
  granted_at: '2026-10-26T15:12:00Z', expires_at: '2026-10-26T15:42:00Z',
}

describe('ActAsYouPanel', () => {
  it('lists live permissions and stops one', async () => {
    list.mockResolvedValueOnce([ROW]).mockResolvedValueOnce([])
    listCanopy.mockResolvedValue([])
    stop.mockResolvedValue(true)
    renderPanel()
    expect(await screen.findByText('connect-labs')).toBeTruthy()
    expect(screen.getByText('ACE')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledWith(7))
    expect(await screen.findByText(/No agent can act as you/)).toBeTruthy()
  })

  it('lists agents acting as you on canopy beside the sites, and stops one', async () => {
    list.mockResolvedValue([])
    listCanopy
      .mockResolvedValueOnce([
        {
          id: 'g1', status: 'approved', scope: 'write', requested_scope: 'either', agent: 'hal',
          agent_name: 'Hal', person: 'jj@dimagi.com', reason: 'Tidy my board', turn_id: 't1',
          session_url: '', expires_at: '2026-10-26T15:42:00Z',
        },
      ])
      .mockResolvedValueOnce([])
    stopCanopy.mockResolvedValue({})
    renderPanel()
    expect(await screen.findByText('Hal')).toBeTruthy()
    expect(screen.getByText('Tidy my board')).toBeTruthy()
    expect(screen.getByText('read + write')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stopCanopy).toHaveBeenCalledWith('g1'))
    expect(await screen.findByText(/No agent is asking or able/)).toBeTruthy()
  })
})
