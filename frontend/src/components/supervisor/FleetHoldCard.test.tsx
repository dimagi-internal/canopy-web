// @vitest-environment jsdom
//
// The fleet controls: the HOLD is a toggle (a superuser turns it on and off; a named
// holder such as Ada may turn it on, never off), and STOP EVERYTHING is a separate
// two-tap action for superusers. Everyone else sees the banner while held — a member
// whose turn sits queued should see why — and nothing when off.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { FleetHoldOut, FleetStopOut } from '@/api/harness'

const holdFleet = vi.fn<(note?: string) => Promise<FleetHoldOut>>()
const releaseFleetHold = vi.fn<() => Promise<FleetHoldOut>>()
const stopFleet = vi.fn<() => Promise<FleetStopOut>>()
vi.mock('@/api/harness', () => ({ holdFleet, releaseFleetHold, stopFleet }))

const { FleetHoldCard } = await import('./FleetHoldCard')

function hold(overrides: Partial<FleetHoldOut> = {}): FleetHoldOut {
  return {
    held: false, note: '', held_at: null, held_by_email: '', queued: 0,
    can_hold: false, can_release: false, can_stop_running: false, ...overrides,
  }
}
const superuser = { can_hold: true, can_release: true, can_stop_running: true }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('FleetHoldCard', () => {
  it('shows nothing to a non-admin while the fleet runs', () => {
    const { container } = render(<FleetHoldCard hold={hold()} onChange={() => {}} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows a non-admin only the banner while held', () => {
    render(<FleetHoldCard hold={hold({ held: true, note: 'tracing', queued: 3 })} onChange={() => {}} />)
    expect(screen.getByTestId('fleet-hold-banner').textContent).toContain('3 turns waiting')
    expect(screen.getByTestId('fleet-hold-banner-why').textContent).toContain('tracing')
    expect(screen.queryByTestId('fleet-hold-toggle')).toBeNull()
  })

  it('turns the hold on in one tap, with the reason', async () => {
    const onChange = vi.fn()
    holdFleet.mockResolvedValue(hold({ ...superuser, held: true, note: 'tracing' }))
    render(<FleetHoldCard hold={hold(superuser)} onChange={onChange} />)

    fireEvent.change(screen.getByTestId('fleet-hold-note'), { target: { value: ' tracing ' } })
    fireEvent.click(screen.getByTestId('fleet-hold-toggle'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ held: true })))
    expect(holdFleet).toHaveBeenCalledWith('tracing')
    expect(stopFleet).not.toHaveBeenCalled()
  })

  it('turns the hold off in one tap', async () => {
    const onChange = vi.fn()
    releaseFleetHold.mockResolvedValue(hold(superuser))
    render(<FleetHoldCard hold={hold({ ...superuser, held: true })} onChange={onChange} />)

    expect(screen.getByTestId('fleet-hold-toggle').getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByTestId('fleet-hold-toggle'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ held: false })))
  })

  it('lets a holder who may not release turn it on but not off, and offers no stop', () => {
    render(<FleetHoldCard hold={hold({ held: true, can_hold: true })} onChange={() => {}} />)
    expect((screen.getByTestId('fleet-hold-toggle') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTestId('fleet-hold-stop-start')).toBeNull()
  })

  it('stops everything as a separate, confirmed action that leaves the hold alone', async () => {
    stopFleet.mockResolvedValue({ stopped_sessions: 2, stopped_turns: 1 })
    const onChange = vi.fn()
    render(<FleetHoldCard hold={hold(superuser)} onChange={onChange} />)

    fireEvent.click(screen.getByTestId('fleet-hold-stop-start'))
    expect(stopFleet).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('fleet-hold-stop-confirm'))

    await waitFor(() =>
      expect(screen.getByTestId('fleet-hold-stopped').textContent).toContain('Stopped 2 working sessions and 1 more running turn'),
    )
    expect(holdFleet).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
  })
})
