// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const list = vi.fn()
const stop = vi.fn()
vi.mock('@/api/hostGrants', () => ({
  listMyHostGrants: () => list(),
  stopMyHostGrant: (id: number) => stop(id),
}))

import { ActAsYouPanel } from './ActAsYouPanel'

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
    stop.mockResolvedValue(true)
    render(<ActAsYouPanel />)
    expect(await screen.findByText('connect-labs')).toBeTruthy()
    expect(screen.getByText('ACE')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledWith(7))
    expect(await screen.findByText(/No agent can act as you/)).toBeTruthy()
  })
})
