// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { Alert } from '@/api/alerts'

const loadAlerts = vi.fn<(scope?: string, status?: 'open' | 'dismissed') => Promise<Alert[]>>()
const actOnAlert = vi.fn<(id: number, action: string) => Promise<Alert>>()
const undismissAlert = vi.fn<(id: number) => Promise<Alert>>()
vi.mock('@/api/alerts', async (orig) => ({
  ...(await orig<typeof import('@/api/alerts')>()),
  loadAlerts: (...a: [string?, ('open' | 'dismissed')?]) => loadAlerts(...a),
  actOnAlert: (id: number, action: string) => actOnAlert(id, action),
  undismissAlert: (id: number) => undismissAlert(id),
}))

const { AlertList } = await import('./AlertList')

const alert = (over: Partial<Alert>): Alert => ({
  id: 1,
  kind: 'runner_credential',
  severity: 'blocking',
  title: 'The "hal" token on cloud-ec2-2 is signed out',
  body: '',
  url: '',
  workspace: 'dimagi',
  organisation: 'dimagi',
  scopes: ['runner:r1', 'workspace:dimagi'],
  status: 'open',
  first_seen_at: new Date().toISOString(),
  last_seen_at: new Date().toISOString(),
  occurrences: 1,
  closed_by_email: '',
  closed_at: null,
  actions: [
    { id: 'remint', label: 'Re-mint token', closes: true, href: '' },
    { id: 'notify_owner', label: 'Ask the owner', closes: false, href: '' },
    { id: 'dismiss', label: 'Dismiss', closes: true, href: '' },
  ],
  last_notify: '',
  handler_agent: '',
  handler_email: '',
  ...over,
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const show = (ui: React.ReactElement) => render(<MemoryRouter>{ui}</MemoryRouter>)

describe('AlertList', () => {
  it('renders one line per alert with closes tags', async () => {
    loadAlerts.mockResolvedValue([
      alert({}),
      alert({ id: 2, kind: 'turn_failure', severity: 'action', title: '4 turns failed on cloud-ec2-2',
        actions: [{ id: 'resend', label: 'Resend your chats', closes: true, href: '' }] }),
    ])
    show(<AlertList scope="workspace:dimagi" />)

    const rows = await screen.findAllByTestId('alert-row')
    expect(rows).toHaveLength(2)
    expect(loadAlerts).toHaveBeenCalledWith('workspace:dimagi', 'open')

    const first = within(rows[0])
    expect(first.getByText('Runner credential')).toBeTruthy()
    expect(first.getByText('The "hal" token on cloud-ec2-2 is signed out')).toBeTruthy()
    expect(within(first.getByRole('button', { name: /Re-mint token/ })).getByText('closes')).toBeTruthy()
    expect(within(first.getByRole('button', { name: /Ask the owner/ })).getByText('stays open')).toBeTruthy()
    // Dismiss is the ×, never a labelled button.
    expect(first.queryByRole('button', { name: /^Dismiss$/ })).toBeNull()
    expect(first.getByRole('button', { name: 'Dismiss for everyone' })).toBeTruthy()

    expect(within(rows[1]).getByText('Failed turns')).toBeTruthy()
    expect(within(rows[1]).queryByRole('button', { name: 'Dismiss for everyone' })).toBeNull()
  })

  it('dismiss removes the row and calls actOnAlert with "dismiss"', async () => {
    loadAlerts.mockResolvedValueOnce([alert({}), alert({ id: 2, title: 'Second' })])
    // Whatever the server says after the dismiss, the row is gone at once.
    let release!: (a: Alert) => void
    actOnAlert.mockReturnValue(new Promise<Alert>((r) => { release = r }))
    loadAlerts.mockResolvedValue([alert({ id: 2, title: 'Second' })])
    show(<AlertList />)

    const rows = await screen.findAllByTestId('alert-row')
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Dismiss for everyone' }))

    expect(actOnAlert).toHaveBeenCalledWith(1, 'dismiss')
    expect(screen.getAllByTestId('alert-row')).toHaveLength(1)
    expect(screen.queryByText('The "hal" token on cloud-ec2-2 is signed out')).toBeNull()
    release(alert({ status: 'dismissed' }))
    await waitFor(() => expect(loadAlerts).toHaveBeenCalledTimes(2))
  })

  it('runs a non-link action and refreshes the list', async () => {
    loadAlerts.mockResolvedValue([alert({})])
    actOnAlert.mockResolvedValue(alert({}))
    show(<AlertList />)

    fireEvent.click(await screen.findByRole('button', { name: /Ask the owner/ }))
    expect(actOnAlert).toHaveBeenCalledWith(1, 'notify_owner')
    await waitFor(() => expect(loadAlerts).toHaveBeenCalledTimes(2))
  })

  it('href actions render as links', async () => {
    loadAlerts.mockResolvedValue([
      alert({ kind: 'agent_cant_run', severity: 'setup', title: 'muse has no runner it can use',
        actions: [{ id: 'route', label: 'Set up routing', closes: false, href: '/w/dimagi/agents/muse/settings' }] }),
    ])
    actOnAlert.mockResolvedValue(alert({}))
    show(<AlertList />)

    const link = await screen.findByRole('link', { name: /Set up routing/ })
    expect(link.getAttribute('href')).toBe('/w/dimagi/agents/muse/settings')
    expect(within(link).getByText('stays open')).toBeTruthy()
    expect(screen.getByText('Agent routing')).toBeTruthy()
    // Following it is logged (fire-and-forget), and the row stays: a link runs nothing.
    fireEvent.click(link)
    expect(actOnAlert).toHaveBeenCalledWith(1, 'route')
  })

  it('hideEmpty renders nothing for no alerts', async () => {
    loadAlerts.mockResolvedValue([])
    const { container } = show(<AlertList scope="agent:muse" hideEmpty />)
    await waitFor(() => expect(loadAlerts).toHaveBeenCalled())
    await Promise.resolve()
    expect(container.innerHTML).toBe('')
  })

  it('counts filter the list by severity', async () => {
    loadAlerts.mockResolvedValue([
      alert({}),
      alert({ id: 2, severity: 'setup', kind: 'device_key_pending', title: 'A device key waits' }),
    ])
    show(<AlertList showCounts />)

    expect(await screen.findAllByTestId('alert-row')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: /Setup\s*1/ }))
    expect(screen.getAllByTestId('alert-row')).toHaveLength(1)
    expect(screen.getByText('A device key waits')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /All\s*2/ }))
    expect(screen.getAllByTestId('alert-row')).toHaveLength(2)
  })

  it('shows who dismissed what in a collapsed fold, with undo', async () => {
    loadAlerts.mockImplementation(async (_scope, status) =>
      status === 'dismissed'
        ? [alert({ id: 9, status: 'dismissed', title: 'Old news', closed_by_email: 'ada@dimagi-ai.com',
            closed_at: new Date().toISOString() })]
        : [],
    )
    undismissAlert.mockResolvedValue(alert({ id: 9 }))
    show(<AlertList showDismissed />)

    const fold = await screen.findByRole('button', { name: /Dismissed \(1\)/ })
    expect(screen.queryByText('Old news')).toBeNull()
    fireEvent.click(fold)
    expect(screen.getByText('Old news')).toBeTruthy()
    expect(screen.getByText(/ada@dimagi-ai\.com/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Undo dismiss: Old news' }))
    expect(undismissAlert).toHaveBeenCalledWith(9)
  })

  it('falls back to All when the filtered severity runs out', async () => {
    const both = [alert({}), alert({ id: 2, severity: 'setup', kind: 'device_key_pending', title: 'A device key waits' })]
    loadAlerts.mockResolvedValue(both)
    actOnAlert.mockResolvedValue(alert({ id: 2, status: 'dismissed' }))
    show(<AlertList showCounts />)

    await screen.findAllByTestId('alert-row')
    fireEvent.click(screen.getByRole('button', { name: /Setup\s*1/ }))
    loadAlerts.mockResolvedValue([both[0]])
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss for everyone' }))
    await waitFor(() => expect(screen.queryByText('A device key waits')).toBeNull())
    // Not an empty list behind a vanished chip: the blocking alert shows again.
    await waitFor(() => expect(screen.getAllByTestId('alert-row')).toHaveLength(1))
    expect(screen.getByText('The "hal" token on cloud-ec2-2 is signed out')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Setup/ })).toBeNull()
  })

  it('keeps an action error through its own refresh and clears it on the next good read', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const { ApiError } = await import('@/api/problem')
      loadAlerts.mockResolvedValue([alert({})])
      actOnAlert.mockRejectedValue(new ApiError(409, 'gone', { type: 'x/action-not-offered' }))
      show(<AlertList />)

      fireEvent.click(await screen.findByRole('button', { name: /Ask the owner/ }))
      expect(await screen.findByRole('alert')).toBeTruthy()
      await waitFor(() => expect(loadAlerts).toHaveBeenCalledTimes(2))
      expect(screen.getByRole('alert').textContent).toContain('no longer yours')
      // The next poll reads fine, and the error goes.
      await vi.advanceTimersByTimeAsync(31_000)
      await waitFor(() => expect(loadAlerts).toHaveBeenCalledTimes(3))
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    } finally {
      vi.useRealTimers()
    }
  })
})
