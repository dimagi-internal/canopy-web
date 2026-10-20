// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { RunnerPairingOut } from '@/api/runnerPairings'

const getRunnerPairing = vi.fn<(code: string) => Promise<RunnerPairingOut>>()
const approveRunnerPairing = vi.fn()
const declineRunnerPairing = vi.fn()

class FakeRunnerPairingError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/runnerPairings', () => ({
  getRunnerPairing,
  approveRunnerPairing,
  declineRunnerPairing,
  RunnerPairingError: FakeRunnerPairingError,
}))

const { PairRunnerPage } = await import('./PairRunnerPage')

function pairing(over: Partial<RunnerPairingOut> = {}): RunnerPairingOut {
  return {
    user_code: 'ABCD-EFGH',
    hostname: 'Jons-MacBook-Pro.local',
    account: 'jj',
    platform: 'macos',
    kind: 'secure-enclave',
    has_emdash: false,
    status: 'pending',
    expires_at: '2026-10-19T11:00:00Z',
    suggested_name: 'jj-jons-macbook-pro',
    workspaces: [
      { slug: 'connect', name: 'Connect' },
      { slug: 'dimagi', name: 'Dimagi' },
    ],
    runner_id: null,
    reconnect_name: '',
    ...over,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/pair/ABCD-EFGH']}>
      <Routes>
        <Route path="/pair/:code" element={<PairRunnerPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('PairRunnerPage', () => {
  it('shows the code to check, what the computer says it is, and the workspaces', async () => {
    getRunnerPairing.mockResolvedValue(pairing())
    renderPage()
    expect((await screen.findByTestId('pairing-code')).textContent).toBe('ABCD-EFGH')
    expect(getRunnerPairing).toHaveBeenCalledWith('ABCD-EFGH')
    expect(screen.getByText(/matches the code Canopy Runner shows/)).toBeTruthy()
    expect(screen.getByText('Jons-MacBook-Pro.local')).toBeTruthy()
    expect(screen.getByText(/Secure Enclave/)).toBeTruthy()
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Connect (connect)', 'Dimagi (dimagi)'])
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('jj-jons-macbook-pro')
  })

  it('approves with the chosen workspace and name, then goes to the computer', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    getRunnerPairing.mockResolvedValue(pairing())
    approveRunnerPairing.mockResolvedValue({
      status: 'approved', redirect_url: 'http://127.0.0.1:53682/paired?pairing=x&secret=y',
    })
    renderPage()
    await screen.findByTestId('pairing-code')
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'dimagi' } })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'jj-laptop' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('http://127.0.0.1:53682/paired?pairing=x&secret=y'))
    expect(approveRunnerPairing).toHaveBeenCalledWith('ABCD-EFGH', { workspace: 'dimagi', name: 'jj-laptop' })
  })

  it('reconnects a runner of yours whose sign-in died, with no name or workspace to pick', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    getRunnerPairing.mockResolvedValue(pairing({ reconnect_name: 'jj-mbp-cdp' }))
    approveRunnerPairing.mockResolvedValue({
      status: 'approved', redirect_url: 'http://127.0.0.1:53682/paired?pairing=x&secret=y',
    })
    renderPage()
    expect((await screen.findByTestId('pairing-reconnect')).textContent).toMatch(/jj-mbp-cdp/)
    expect(screen.queryByRole('combobox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(assign).toHaveBeenCalled())
  })

  it('shows why an approval was refused', async () => {
    getRunnerPairing.mockResolvedValue(pairing())
    approveRunnerPairing.mockRejectedValue(new FakeRunnerPairingError(409, "you already have a runner named 'x'"))
    renderPage()
    await screen.findByTestId('pairing-code')
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText(/already have a runner named/)).toBeTruthy()
  })

  it('declines', async () => {
    getRunnerPairing.mockResolvedValue(pairing())
    declineRunnerPairing.mockResolvedValue(undefined)
    renderPage()
    await screen.findByTestId('pairing-code')
    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(screen.getByTestId('pairing-status').textContent).toMatch(/^Declined/))
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('offers nothing to approve once used, and says so', async () => {
    getRunnerPairing.mockResolvedValue(pairing({ status: 'consumed' }))
    renderPage()
    expect((await screen.findByTestId('pairing-status')).textContent).toMatch(/paired/)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('without a workspace to run work in, explains instead of offering Approve', async () => {
    getRunnerPairing.mockResolvedValue(pairing({ workspaces: [] }))
    renderPage()
    expect(await screen.findByText(/cannot pair a runner/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('an unknown code says so', async () => {
    getRunnerPairing.mockRejectedValue(new FakeRunnerPairingError(404, 'pairing not found'))
    renderPage()
    expect(await screen.findByText(/No pairing has this code/)).toBeTruthy()
  })
})
