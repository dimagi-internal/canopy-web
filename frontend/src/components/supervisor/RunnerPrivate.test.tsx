// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// The owner turns their own runner private from the runner's page — no CLI on the
// box (canopy-web#1705). Acting as a lower role is a turn's, not a machine's.

const setRunnerPrivate = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerPrivate: (...a: unknown[]) => setRunnerPrivate(...a),
}))

const { RunnerPrivate } = await import('./RunnerPrivate')

const shared = { id: 'r1', name: 'jj-mbp-cdp', private: false } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerPrivate', () => {
  it('shows a shared runner as shared, with no testing control', () => {
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    expect(screen.getByRole('radio', { name: 'Shared' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByText('Test as a lower role')).toBeNull()
  })

  it('makes it private as yourself', async () => {
    setRunnerPrivate.mockResolvedValue({ ...shared, private: true })
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Private' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', true))
  })

  it('puts it back in the fleet, and never offers a role to act as', async () => {
    setRunnerPrivate.mockResolvedValue(shared)
    const priv = { ...shared, private: true } as unknown as RunnerOut
    render(<RunnerPrivate runner={priv} onChange={onChange} />)
    expect(screen.queryByText('Test as a lower role')).toBeNull()
    expect(screen.queryByRole('radio', { name: 'Editor' })).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'Shared' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', false))
  })

  it('shows the server error', async () => {
    setRunnerPrivate.mockRejectedValue(new Error('only the runner\'s owner can make it private'))
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Private' }))
    expect(await screen.findByText(/only the runner's owner/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })
})
