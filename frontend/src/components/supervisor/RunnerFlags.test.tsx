// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A box's owner declares what it guarantees (a sensitive-data box: only a ZDR API key,
// a locked harness). canopy cannot check it, so the panel has to say so where the
// box is ticked, not in a help page.

const setRunnerFlags = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerFlags: (...a: unknown[]) => setRunnerFlags(...a),
}))

const { RunnerFlags } = await import('./RunnerFlags')

const runner = { id: 'r1', name: 'cloud-1', flags: [], known_flags: ['sensitive_data'] } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerFlags', () => {
  it('heads the panel with who is speaking, not a guarantee canopy cannot make', () => {
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    expect(screen.getByText(/declared by the owner/)).toBeTruthy()
    expect(screen.queryByText(/Guarantees/i)).toBeNull()
  })

  it('declares sensitive_data with the vouching sentence visible', async () => {
    const updated = { ...runner, flags: ['sensitive_data'] } as unknown as RunnerOut
    setRunnerFlags.mockResolvedValue(updated)
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    expect(screen.getByText(/You are vouching for this; canopy cannot check the key/)).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: /Sensitive data/ }))
    await waitFor(() => expect(setRunnerFlags).toHaveBeenCalledWith('r1', ['sensitive_data']))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(updated))
  })

  it('says a declared sensitive_data is not in force while the box does not report sensitive-data mode', () => {
    render(<RunnerFlags runner={{ ...runner, flags: ['sensitive_data'], capabilities: {} } as unknown as RunnerOut}
                        onChange={onChange} />)
    expect(screen.getByTestId('runner-flag-not-in-force-sensitive_data')).toBeTruthy()
  })

  it('says nothing more once the box reports sensitive-data mode, or when sensitive_data is not declared', () => {
    render(<RunnerFlags runner={{ ...runner, flags: ['sensitive_data'], capabilities: { data_mode: 'sensitive_data' } } as unknown as RunnerOut}
                        onChange={onChange} />)
    expect(screen.queryByTestId('runner-flag-not-in-force-sensitive_data')).toBeNull()
    cleanup()
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    expect(screen.queryByTestId('runner-flag-not-in-force-sensitive_data')).toBeNull()
  })

  it('withdraws it', async () => {
    setRunnerFlags.mockResolvedValue({ ...runner, flags: [] })
    render(<RunnerFlags runner={{ ...runner, flags: ['sensitive_data'] } as unknown as RunnerOut} onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch', { name: /Sensitive data/ }))
    await waitFor(() => expect(setRunnerFlags).toHaveBeenCalledWith('r1', []))
  })

  it('shows the server error', async () => {
    setRunnerFlags.mockRejectedValue(new Error('unknown flag'))
    render(<RunnerFlags runner={runner} onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch', { name: /Sensitive data/ }))
    expect(await screen.findByText(/unknown flag/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('draws one switch per known flag from the server, not a local list', () => {
    render(
      <RunnerFlags
        runner={{ ...runner, known_flags: ['sensitive_data', 'eu'] } as unknown as RunnerOut}
        onChange={onChange}
      />,
    )
    expect(screen.getAllByRole('switch')).toHaveLength(2)
  })
})
