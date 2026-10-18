// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const row = {
  claude_session: 'abc', repo: 'canopy-web', title: 'Fix the login', first_at: null, last_at: '2026-10-01T00:00:00Z',
  days: 1, input_tokens: 10, output_tokens: 20, cache_read_tokens: 3000, cache_creation_tokens: 0,
  files_in: 'dimagi', agent: '',
}
const list = vi.fn()
const send = vi.fn(async () => ['s1'])
vi.mock('@/api/harness', () => ({
  listMissingSessions: (...a: unknown[]) => list(...a),
  checkMissingSessions: vi.fn(),
  sendUpMissingSessions: (...a: unknown[]) => send(...(a as [])),
}))

const { MissingSessions } = await import('./MissingSessions')

afterEach(() => cleanup())

describe('MissingSessions', () => {
  it('lists what the box has that canopy does not, and sends up only what was chosen', async () => {
    list.mockResolvedValueOnce({ runner: 'laptop', sessions: [row] })
        .mockResolvedValueOnce({ runner: 'laptop', sessions: [] })
    render(<MissingSessions runnerId="r1" />)
    expect(await screen.findByText('Fix the login')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Select Fix the login'))
    fireEvent.click(screen.getByRole('button', { name: /Send up 1 selected/ }))
    await waitFor(() => expect(screen.getByText(/Sent up 1 session/)).toBeTruthy())
    expect(send).toHaveBeenCalledWith('r1', ['abc'])
    expect(screen.getByText('Canopy has every session this box reported.')).toBeTruthy()
  })
})
