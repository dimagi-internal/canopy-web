// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// The owner turns their own runner private, and picks the role it acts as, from the
// runner's page — no CLI on the box (canopy-web#1705).

const setRunnerPrivate = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerPrivate: (...a: unknown[]) => setRunnerPrivate(...a),
}))

const { RunnerPrivate } = await import('./RunnerPrivate')

const shared = { id: 'r1', name: 'jj-mbp-cdp', private: false, acts_as: '' } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerPrivate', () => {
  it('shows a shared runner as shared', () => {
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    expect(screen.getByRole('radio', { name: 'Shared' }).getAttribute('aria-checked')).toBe('true')
  })

  it('makes it private, acting as an editor', async () => {
    const updated = { ...shared, private: true, acts_as: 'editor' } as unknown as RunnerOut
    setRunnerPrivate.mockResolvedValue(updated)
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Private · as editor' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', true, 'editor'))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(updated))
  })

  it('puts it back in the fleet', async () => {
    setRunnerPrivate.mockResolvedValue(shared)
    const priv = { ...shared, private: true, acts_as: 'viewer' } as unknown as RunnerOut
    render(<RunnerPrivate runner={priv} onChange={onChange} />)
    expect(screen.getByRole('radio', { name: 'Private · as viewer' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'Shared' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', false, ''))
  })

  it('shows the server error', async () => {
    setRunnerPrivate.mockRejectedValue(new Error('only the runner\'s owner can make it private'))
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Private · me' }))
    expect(await screen.findByText(/only the runner's owner/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })
})
