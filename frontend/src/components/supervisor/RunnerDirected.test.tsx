// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A runner admin turns "directed only" on or off from the runner page.

const setRunnerDirected = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerDirected: (...a: unknown[]) => setRunnerDirected(...a),
}))

const { RunnerDirected } = await import('./RunnerDirected')

const box = { id: 'r1', name: 'cloud-zdr-1', private: false, directed: true } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerDirected', () => {
  it('turns a directed box off', async () => {
    setRunnerDirected.mockResolvedValue({ ...box, directed: false })
    render(<RunnerDirected runner={box} onChange={onChange} />)
    expect(screen.getByText(/no routed work/)).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: 'Directed only' }))
    await waitFor(() => expect(setRunnerDirected).toHaveBeenCalledWith('r1', false))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
  })

  it('turns it back on', async () => {
    setRunnerDirected.mockResolvedValue(box)
    render(<RunnerDirected runner={{ ...box, directed: false } as unknown as RunnerOut} onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Directed only' }))
    await waitFor(() => expect(setRunnerDirected).toHaveBeenCalledWith('r1', true))
  })

  it('cannot be turned off on a private runner', () => {
    const priv = { ...box, private: true } as unknown as RunnerOut
    render(<RunnerDirected runner={priv} onChange={onChange} />)
    expect((screen.getByRole('switch', { name: 'Directed only' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
