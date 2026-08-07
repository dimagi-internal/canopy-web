// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthContext } from '@/auth/AuthProvider'
import { WalkthroughViewerPage } from './WalkthroughViewerPage'
import * as api from '@/api/walkthroughs'

vi.mock('@/api/walkthroughs', async () => {
  const actual = await vi.importActual<typeof api>('@/api/walkthroughs')
  return { ...actual, getWalkthrough: vi.fn() }
})

const getWalkthrough = api.getWalkthrough as unknown as ReturnType<typeof vi.fn>

type AuthState = React.ContextType<typeof AuthContext>

const ANON: AuthState = { status: 'anonymous', user: null }
const AUTHED = {
  status: 'authenticated',
  user: { email: 'hal@dimagi-ai.com' },
} as unknown as AuthState

function renderAt(path: string, auth: AuthState) {
  return render(
    <AuthContext.Provider value={auth}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/walkthrough/:id" element={<WalkthroughViewerPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
})

describe('WalkthroughViewerPage — anonymous visitor on a private walkthrough (#516)', () => {
  it('offers sign-in rather than a bare error when the detail fetch 404s', async () => {
    getWalkthrough.mockRejectedValue(new api.WalkthroughApiError(404, 'Not found'))

    renderAt('/walkthrough/w-1', ANON)

    const link = await screen.findByRole('link', { name: /sign in with google/i })
    expect(link.getAttribute('href')).toContain('/accounts/google/login/')
    // The whole point of the fix: they land back on the walkthrough afterwards.
    expect(link.getAttribute('href')).toContain('next=')
    expect(screen.queryByText(/Failed to load walkthrough/i)).toBeNull()
  })

  it.each([401, 403])('treats %i the same as 404 — all mean "you cannot see this"', async (status) => {
    getWalkthrough.mockRejectedValue(new api.WalkthroughApiError(status, 'nope'))

    renderAt('/walkthrough/w-1', ANON)

    expect(await screen.findByRole('link', { name: /sign in with google/i })).toBeTruthy()
  })

  it('says the share link is stale when one was supplied and still refused', async () => {
    // A ?t= holder is NOT simply logged out — their token was rotated or is
    // wrong, so "this is shared with Dimagi" would be the wrong explanation.
    window.history.replaceState({}, '', '/walkthrough/w-1?t=stale-token')
    getWalkthrough.mockRejectedValue(new api.WalkthroughApiError(404, 'Not found'))

    renderAt('/walkthrough/w-1?t=stale-token', ANON)

    expect(await screen.findByText(/no longer valid/i)).toBeTruthy()
  })

  it('does NOT offer sign-in for a transient failure — that would be a lie', async () => {
    // A network drop is not an access problem; sending the user through OAuth
    // would "fix" it only by coincidence of the reload.
    getWalkthrough.mockRejectedValue(new Error('Failed to fetch'))

    renderAt('/walkthrough/w-1', ANON)

    await waitFor(() => expect(screen.getByText(/Failed to fetch/)).toBeTruthy())
    expect(screen.queryByRole('link', { name: /sign in with google/i })).toBeNull()
  })
})

describe('WalkthroughViewerPage — signed-in visitor', () => {
  it('names both live possibilities instead of offering a pointless re-login', async () => {
    getWalkthrough.mockRejectedValue(new api.WalkthroughApiError(404, 'Not found'))

    renderAt('/walkthrough/w-1', AUTHED)

    expect(await screen.findByText(/Walkthrough not available/i)).toBeTruthy()
    expect(screen.queryByRole('link', { name: /sign in with google/i })).toBeNull()
  })
})
