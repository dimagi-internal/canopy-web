// @vitest-environment jsdom
// Confirming a page's act-as-you permission in the browser, for a panel that cannot
// use a passkey (the Canopy Runner app opens this page there).
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getMyHostGrant: vi.fn(), confirmMyHostGrant: vi.fn(), stopMyHostGrant: vi.fn() }))
vi.mock('@/api/hostGrants', () => api)

import { PagePermissionConfirmPage } from './PagePermissionConfirmPage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const GRANT = {
  id: 7, site: 'canopy-web', agent: 'ada', agent_name: 'Ada', scope: 'sessions:close',
  granted_at: '2026-10-29T08:00:00Z', expires_at: '2026-10-29T08:30:00Z', confirmed: false,
}

function open() {
  return render(
    <MemoryRouter initialEntries={['/confirm/page-permission/7']}>
      <Routes>
        <Route path="/confirm/page-permission/:grantId" element={<PagePermissionConfirmPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('PagePermissionConfirmPage', () => {
  it('asks the panel’s question and confirms the same grant with a passkey', async () => {
    api.getMyHostGrant.mockResolvedValue(GRANT)
    api.confirmMyHostGrant.mockResolvedValue({ ...GRANT, confirmed: true })
    open()
    expect(await screen.findByText('Let Ada act as you on canopy-web for 30 minutes?')).toBeTruthy()
    fireEvent.click(screen.getByTestId('page-permission-confirm'))
    expect(await screen.findByTestId('page-permission-confirmed')).toBeTruthy()
    expect(api.confirmMyHostGrant).toHaveBeenCalledWith(7)
  })

  it('Not now stops it', async () => {
    api.getMyHostGrant.mockResolvedValue(GRANT)
    api.stopMyHostGrant.mockResolvedValue(true)
    open()
    fireEvent.click(await screen.findByText('Not now'))
    expect(await screen.findByTestId('page-permission-stopped')).toBeTruthy()
    expect(api.stopMyHostGrant).toHaveBeenCalledWith(7)
  })

  it('says so when the permission is no longer waiting', async () => {
    api.getMyHostGrant.mockRejectedValue(new Error('404'))
    open()
    expect(await screen.findByTestId('page-permission-gone')).toBeTruthy()
  })
})
