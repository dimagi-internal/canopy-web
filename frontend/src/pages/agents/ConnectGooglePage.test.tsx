// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const start = vi.fn()
const get = vi.fn()
vi.mock('@/api/agents', () => ({
  startGoogleDelegation: (...a: unknown[]) => start(...a),
  getGoogleDelegation: (...a: unknown[]) => get(...a),
}))

import { ConnectGooglePage, delegationOutcome, safeReturn, servicesFrom } from './ConnectGooglePage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function at(url: string) {
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/connect/google" element={<ConnectGooglePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ConnectGooglePage', () => {
  it('reads the services a page asked for, defaulting to Gmail', () => {
    expect(servicesFrom(null)).toEqual(['gmail'])
    expect(servicesFrom('calendar,gmail,youtube')).toEqual(['gmail', 'calendar'])
  })

  it('starts a delegation for the account the person names', async () => {
    start.mockResolvedValue({ id: 'm1', status: 'requested', runner: 'cloud-ec2-2' })
    get.mockResolvedValue({ id: 'm1', status: 'failed', authorize_url: '', detail: 'no runner' })
    at('/w/marshellis/agents/bailey/connect/google?services=gmail')
    fireEvent.change(screen.getByPlaceholderText('you@gmail.com'), { target: { value: 'gina@gmail.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect with Google' }))
    await waitFor(() => expect(start).toHaveBeenCalledWith('bailey', 'gina@gmail.com', ['gmail']))
    expect(await screen.findByTestId('connect-google-error')).toBeTruthy()
  })

  it('follows the sign-in to its end after Google sends the person back', async () => {
    get.mockResolvedValue({ id: 'm1', status: 'done', outcome: 'ok', account: 'gina@gmail.com', expected_account: 'gina@gmail.com' })
    at('/w/marshellis/agents/bailey/connect/google?google=pending&mint=m1')
    expect(await screen.findByText(/Bailey can now use gina@gmail.com/)).toBeTruthy()
  })

  it('offers only the services the page asked for', () => {
    at('/w/marshellis/agents/bailey/connect/google?services=gmail')
    expect(screen.getByLabelText('Gmail')).toBeTruthy()
    expect(screen.queryByLabelText('Calendar')).toBeNull()
    expect(screen.getByText(/also allows sending/)).toBeTruthy()
  })

  it('sends the person back where they came from once connected', async () => {
    sessionStorage.clear()
    at('/w/marshellis/agents/bailey/connect/google?services=gmail&return=/w/marshellis/views/2')
    cleanup()
    get.mockResolvedValue({ id: 'm1', status: 'done', outcome: 'ok', account: 'gina@gmail.com', expected_account: 'gina@gmail.com' })
    at('/w/marshellis/agents/bailey/connect/google?google=pending&mint=m1')
    const back = await screen.findByTestId('connect-google-back')
    expect(back.getAttribute('href')).toBe('/w/marshellis/views/2')
  })

  it('never sends the person to another site', () => {
    expect(safeReturn('https://evil.example')).toBeNull()
    expect(safeReturn('//evil.example')).toBeNull()
    expect(safeReturn('/w/marshellis/views/2')).toBe('/w/marshellis/views/2')
  })

  it('says why nothing was stored', () => {
    expect(delegationOutcome('not-admin', { slug: 'bailey' })?.text).toMatch(/no longer an admin/)
    expect(delegationOutcome('wrong-account', { slug: 'bailey', account: 'x@gmail.com', expected: 'gina@gmail.com' })?.text)
      .toMatch(/x@gmail.com, not gina@gmail.com/)
  })
})
