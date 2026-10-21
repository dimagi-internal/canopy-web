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
  it('shows a shared runner as shared, with no testing control', () => {
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    expect(screen.getByRole('radio', { name: 'Shared' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByText('Test as a lower role')).toBeNull()
  })

  it('makes it private as yourself', async () => {
    setRunnerPrivate.mockResolvedValue({ ...shared, private: true })
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Private' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', true, ''))
  })

  it('tests a private runner as an editor, and says so', async () => {
    const priv = { ...shared, private: true } as unknown as RunnerOut
    const updated = { ...priv, acts_as: 'editor' } as unknown as RunnerOut
    setRunnerPrivate.mockResolvedValue(updated)
    const { rerender } = render(<RunnerPrivate runner={priv} onChange={onChange} />)
    expect(screen.queryByTestId('runner-test-role-on')).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'Editor' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', true, 'editor'))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(updated))
    rerender(<RunnerPrivate runner={updated} onChange={onChange} />)
    expect(screen.getByTestId('runner-test-role-on').textContent).toMatch(/judged as an editor/)
  })

  it('puts it back in the fleet', async () => {
    setRunnerPrivate.mockResolvedValue(shared)
    const priv = { ...shared, private: true, acts_as: 'viewer' } as unknown as RunnerOut
    render(<RunnerPrivate runner={priv} onChange={onChange} />)
    expect(screen.getByRole('radio', { name: 'Viewer' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'Shared' }))
    await waitFor(() => expect(setRunnerPrivate).toHaveBeenCalledWith('r1', false, ''))
  })

  it('shows the server error', async () => {
    setRunnerPrivate.mockRejectedValue(new Error('only the runner\'s owner can make it private'))
    render(<RunnerPrivate runner={shared} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Private' }))
    expect(await screen.findByText(/only the runner's owner/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })
})
