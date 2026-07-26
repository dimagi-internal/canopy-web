// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

// Same hoist-then-dynamic-import pattern as InviteAcceptPage.test.tsx.
const getMe = vi.fn()
const bootstrapCsrf = vi.fn(async () => undefined)
const isLoginBounceInFlight = vi.fn(() => false)
const noteAuthSucceeded = vi.fn()

vi.mock('@/api/me', () => ({ getMe }))
vi.mock('@/api/csrf', () => ({ bootstrapCsrf }))
vi.mock('@/api/client.v2', () => ({ isLoginBounceInFlight, noteAuthSucceeded }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  isLoginBounceInFlight.mockReturnValue(false)
})

/**
 * The Android flicker (2026-07-26): our own Sign-in button painting for a few
 * hundred ms and then being swept away by a full-page navigation to Google.
 *
 * The cause is a race between two things that both follow a 401 on /api/me:
 * the onResponse middleware commits to `window.location.href = …` (which the
 * browser performs asynchronously), and AuthProvider transitions to `anonymous`
 * and renders. Both happen; the render lands first and is then discarded.
 *
 * Reproducing the *flicker* itself would mean asserting on paint timing. What
 * these tests pin instead is the decision underneath it — given a bounce is
 * committed, we must not render a Sign-in button the user cannot usefully
 * click. That is the part a future refactor could silently undo.
 *
 * Only reachable at all because the service worker serves /supervisor from its
 * precache (it is on NAVIGATE_FALLBACK_ALLOWLIST), so Django's own 302-to-login
 * never runs and the SPA boots anonymous. Without the SW the server redirects
 * first and none of this renders.
 */
describe('AuthProvider anonymous rendering', () => {
  async function renderProvider() {
    const { AuthProvider } = await import('./AuthProvider')
    render(
      <AuthProvider>
        <div>protected content</div>
      </AuthProvider>,
    )
  }

  it('shows continuity, not a Sign-in button, while a bounce to Google is in flight', async () => {
    getMe.mockResolvedValue(null)
    isLoginBounceInFlight.mockReturnValue(true)

    await renderProvider()

    await waitFor(() => expect(screen.getByText('Signing in…')).toBeTruthy())
    // The flicker itself: a button offering an action that is about to be
    // yanked out from under the user.
    expect(screen.queryByText('Sign in with Google')).toBeNull()
    expect(screen.queryByText('protected content')).toBeNull()
  })

  it('shows the Sign-in button when no bounce is in flight — the loop guard held', async () => {
    getMe.mockResolvedValue(null)
    isLoginBounceInFlight.mockReturnValue(false)

    await renderProvider()

    // The case that matters most: we came back from a FAILED round-trip and the
    // guard declined to navigate again. Nothing else will move now, so hiding
    // the button behind "Signing in…" would strand the user on a dead screen.
    await waitFor(() => expect(screen.getByText('Sign in with Google')).toBeTruthy())
    expect(screen.queryByText('Signing in…')).toBeNull()
  })

  it('renders children and clears the loop guard once authenticated', async () => {
    getMe.mockResolvedValue({ email: 'a@dimagi.com', name: 'A', avatar_url: null })

    await renderProvider()

    await waitFor(() => expect(screen.getByText('protected content')).toBeTruthy())
    expect(noteAuthSucceeded).toHaveBeenCalled()
  })
})
