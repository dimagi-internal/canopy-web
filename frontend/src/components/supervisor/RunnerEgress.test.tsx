// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A runner admin locks a box's egress and names extra hosts from the runner's page.

const setRunnerEgress = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerEgress: (...a: unknown[]) => setRunnerEgress(...a),
}))

const { RunnerEgress } = await import('./RunnerEgress')

const open = { id: 'r1', name: 'cloud-ec2-2', kind: 'cloud', egress_locked: false, egress_allow: [],
  egress_forced: false } as unknown as RunnerOut
const zdr = { ...open, name: 'cloud-zdr-1', egress_forced: true } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerEgress', () => {
  it('says an open box reaches any host', () => {
    render(<RunnerEgress runner={open} onChange={onChange} />)
    expect(screen.getByText(/reach any host/)).toBeTruthy()
    expect((screen.getByTestId('runner-egress-save') as HTMLButtonElement).disabled).toBe(true)
  })

  it('locks it with extra hosts', async () => {
    setRunnerEgress.mockResolvedValue({ ...open, egress_locked: true, egress_allow: ['slack.com'] })
    render(<RunnerEgress runner={open} onChange={onChange} />)
    fireEvent.click(screen.getByTestId('runner-egress-locked'))
    fireEvent.change(screen.getByTestId('runner-egress-allow'), { target: { value: 'slack.com' } })
    fireEvent.click(screen.getByTestId('runner-egress-save'))
    await waitFor(() => expect(setRunnerEgress).toHaveBeenCalledWith('r1', true, ['slack.com']))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
  })

  it('shows a ZDR box locked, and the lock cannot be turned off', () => {
    render(<RunnerEgress runner={zdr} onChange={onChange} />)
    const box = screen.getByTestId('runner-egress-locked') as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(box.disabled).toBe(true)
    expect(screen.getByText(/Always locked on a ZDR box/)).toBeTruthy()
  })

  it('shows the server error', async () => {
    setRunnerEgress.mockRejectedValue(new Error("'bad/host' is not a hostname"))
    render(<RunnerEgress runner={open} onChange={onChange} />)
    fireEvent.change(screen.getByTestId('runner-egress-allow'), { target: { value: 'bad/host' } })
    fireEvent.click(screen.getByTestId('runner-egress-save'))
    expect(await screen.findByText(/is not a hostname/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })
})
