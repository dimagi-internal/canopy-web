// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A box's owner declares what it guarantees (ZDR keys only). canopy cannot check
// it, so the panel has to say so where the box is ticked, not in a help page.

const setRunnerFlags = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerFlags: (...a: unknown[]) => setRunnerFlags(...a),
}))

const { RunnerFlags } = await import('./RunnerFlags')

const runner = { id: 'r1', name: 'cloud-1', flags: [], known_flags: ['zdr'] } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerFlags', () => {
  it('heads the panel with who is speaking, not a guarantee canopy cannot make', () => {
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    expect(screen.getByText('Declared by the owner')).toBeTruthy()
    expect(screen.queryByText(/Guarantees/i)).toBeNull()
  })

  it('declares zdr with the vouching sentence visible', async () => {
    const updated = { ...runner, flags: ['zdr'] } as unknown as RunnerOut
    setRunnerFlags.mockResolvedValue(updated)
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    expect(screen.getByText(/You are vouching for this; canopy cannot check it/)).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: /ZDR/ }))
    await waitFor(() => expect(setRunnerFlags).toHaveBeenCalledWith('r1', ['zdr']))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(updated))
  })

  it('withdraws it', async () => {
    setRunnerFlags.mockResolvedValue({ ...runner, flags: [] })
    render(<RunnerFlags runner={{ ...runner, flags: ['zdr'] } as unknown as RunnerOut} onChange={onChange} />)
    fireEvent.click(screen.getByRole('checkbox', { name: /ZDR/ }))
    await waitFor(() => expect(setRunnerFlags).toHaveBeenCalledWith('r1', []))
  })

  it('shows the server error', async () => {
    setRunnerFlags.mockRejectedValue(new Error('unknown flag'))
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    fireEvent.click(screen.getByRole('checkbox', { name: /ZDR/ }))
    expect(await screen.findByText(/unknown flag/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('draws one checkbox per known flag from the server, not a local list', () => {
    render(
      <RunnerFlags
        runner={{ ...runner, known_flags: ['zdr', 'eu'] } as unknown as RunnerOut}
        onChange={onChange}
      />,
    )
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })
})
