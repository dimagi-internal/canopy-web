// @vitest-environment jsdom
//
// The fleet hold stops every runner in every workspace, so the control is for
// system admins (`can_hold`, and `can_release` = superuser) — a named holder such as
// Ada may hold but not release. Everyone else sees the banner while
// it is on — a member whose turn sits queued should see why — and nothing when off.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { FleetHoldOut } from '@/api/harness'

const holdFleet = vi.fn<(note?: string, stopRunning?: boolean) => Promise<FleetHoldOut>>()
const releaseFleetHold = vi.fn<() => Promise<FleetHoldOut>>()
vi.mock('@/api/harness', () => ({ holdFleet, releaseFleetHold }))

const { FleetHoldCard } = await import('./FleetHoldCard')

function hold(overrides: Partial<FleetHoldOut> = {}): FleetHoldOut {
  return { held: false, note: '', held_at: null, held_by_email: '', queued: 0, can_hold: false, can_release: false, can_stop_running: false, stopped_sessions: 0, stopped_turns: 0, ...overrides }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('FleetHoldCard', () => {
  it('shows nothing to a non-admin while the fleet runs', () => {
    const { container } = render(<FleetHoldCard hold={hold()} onChange={() => {}} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows a non-admin the banner, without a release button, while held', () => {
    render(<FleetHoldCard hold={hold({ held: true, note: 'tracing', queued: 3 })} onChange={() => {}} />)
    expect(screen.getByTestId('fleet-hold-banner').textContent).toContain('3 turns waiting')
    expect(screen.getByTestId('fleet-hold-why').textContent).toContain('tracing')
    expect(screen.queryByTestId('fleet-hold-release')).toBeNull()
  })

  it('makes an admin confirm before holding, and sends the reason', async () => {
    const onChange = vi.fn()
    holdFleet.mockResolvedValue(hold({ held: true, can_hold: true, note: 'tracing' }))
    render(<FleetHoldCard hold={hold({ can_hold: true })} onChange={onChange} />)

    fireEvent.change(screen.getByTestId('fleet-hold-note'), { target: { value: ' tracing ' } })
    fireEvent.click(screen.getByTestId('fleet-hold-start'))
    expect(holdFleet).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('fleet-hold-confirm'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ held: true })))
    expect(holdFleet).toHaveBeenCalledWith('tracing')
  })

  it('lets an admin release in one tap', async () => {
    const onChange = vi.fn()
    releaseFleetHold.mockResolvedValue(hold({ can_hold: true, can_release: true }))
    render(<FleetHoldCard hold={hold({ held: true, can_hold: true, can_release: true })} onChange={onChange} />)

    fireEvent.click(screen.getByTestId('fleet-hold-release'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ held: false })))
  })

  it('offers a superuser the kill switch, which also stops running work, and reports what it stopped', async () => {
    const onChange = vi.fn()
    holdFleet.mockResolvedValue(hold({ held: true, can_hold: true, can_stop_running: true, stopped_sessions: 2, stopped_turns: 1 }))
    render(<FleetHoldCard hold={hold({ can_hold: true, can_stop_running: true })} onChange={onChange} />)

    fireEvent.click(screen.getByTestId('fleet-hold-start'))
    fireEvent.click(screen.getByTestId('fleet-hold-stop-all'))

    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(holdFleet).toHaveBeenCalledWith('', true)
    cleanup()
    render(<FleetHoldCard hold={onChange.mock.calls[0][0]} onChange={() => {}} />)
    expect(screen.getByTestId('fleet-hold-stopped').textContent).toContain('Stopped 2 working sessions and 1 more running turn')
  })

  it('offers a named holder no kill switch', () => {
    render(<FleetHoldCard hold={hold({ can_hold: true })} onChange={() => {}} />)
    fireEvent.click(screen.getByTestId('fleet-hold-start'))
    expect(screen.queryByTestId('fleet-hold-stop-all')).toBeNull()
  })

  it('offers a holder who may not release no Release button', () => {
    render(<FleetHoldCard hold={hold({ held: true, can_hold: true, can_release: false })} onChange={() => {}} />)

    expect(screen.getByTestId('fleet-hold-banner')).toBeTruthy()
    expect(screen.queryByTestId('fleet-hold-release')).toBeNull()
  })
})
