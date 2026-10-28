// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'

const getGoogleDelegation = vi.fn()
vi.mock('@/api/agents', () => ({
  startGoogleDelegation: vi.fn(async () => ({ id: 'm1' })),
  getGoogleDelegation: (...a: unknown[]) => getGoogleDelegation(...a),
}))
vi.mock('@/api/client.v2', () => ({
  apiV2: { GET: vi.fn(async () => ({ response: { ok: true }, data: [
    { id: 7, agent: 'bailey', provider: 'google', account: 'gina@gmail.com', in_force: true, session_until: '2026-10-28T17:00:00Z' },
  ] })) },
}))
vi.mock('@/pages/agents/googleMint', () => ({
  followMint: async <T,>(read: () => Promise<T>, done: (m: T) => boolean) => {
    for (;;) { const m = await read(); if (done(m)) return m }
  },
}))

import { GoogleAccessDialog } from './googleAccess'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('GoogleAccessDialog', () => {
  it('keeps waiting when Google cuts the window off (closed reads true mid sign-in)', async () => {
    // Google's sign-in page severs the popup, so `closed` is true while the person signs in.
    vi.spyOn(window, 'open').mockReturnValue({ closed: true, close: vi.fn(), location: { href: 'about:blank' } } as unknown as Window)
    getGoogleDelegation
      .mockResolvedValueOnce({ id: 'm1', status: 'awaiting', authorize_url: 'https://accounts.google.com/x' })
      .mockResolvedValueOnce({ id: 'm1', status: 'awaiting', authorize_url: 'https://accounts.google.com/x' })
      .mockResolvedValueOnce({ id: 'm1', status: 'completing' })
      .mockResolvedValueOnce({ id: 'm1', status: 'done', outcome: 'ok', account: 'gina@gmail.com' })
    const onDone = vi.fn()
    const ui = render(<GoogleAccessDialog onDone={onDone}
      ask={{ agent: 'bailey', account: 'gina@gmail.com', services: ['gmail'], minutes: 60, purpose: '' }} />)
    fireEvent.click(ui.getByText('Continue to Google'))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(onDone).toHaveBeenCalledWith({ ok: true, account: 'gina@gmail.com', until: '2026-10-28T17:00:00Z', outcome: 'ok' })
    expect(ui.queryByTestId('google-access-error')).toBeNull()
  })
})
