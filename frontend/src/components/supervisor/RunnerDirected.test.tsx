// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A runner admin turns "directed only" off on a box that declared it — no shell.

const setRunnerDirected = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerDirected: (...a: unknown[]) => setRunnerDirected(...a),
}))

const { RunnerDirected } = await import('./RunnerDirected')

const box = {
  id: 'r1', name: 'cloud-zdr-1', private: false, directed: true, directed_setting: null,
  capabilities: { directed: true },
} as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerDirected', () => {
  it('says the box declares it, and turns it off', async () => {
    setRunnerDirected.mockResolvedValue({ ...box, directed: false, directed_setting: false })
    render(<RunnerDirected runner={box} onChange={onChange} />)
    expect(screen.getByText(/The box declares it/)).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: 'Directed only' }))
    await waitFor(() => expect(setRunnerDirected).toHaveBeenCalledWith('r1', false))
  })

  it('offers going back to the box’s own setting once overridden', async () => {
    setRunnerDirected.mockResolvedValue(box)
    const off = { ...box, directed: false, directed_setting: false } as unknown as RunnerOut
    render(<RunnerDirected runner={off} onChange={onChange} />)
    expect(screen.getByText(/the box itself says directed/)).toBeTruthy()
    fireEvent.click(screen.getByTestId('runner-directed-follow-box'))
    await waitFor(() => expect(setRunnerDirected).toHaveBeenCalledWith('r1', null))
  })

  it('cannot be turned off on a private runner', () => {
    const priv = { ...box, private: true } as unknown as RunnerOut
    render(<RunnerDirected runner={priv} onChange={onChange} />)
    expect((screen.getByRole('switch', { name: 'Directed only' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
