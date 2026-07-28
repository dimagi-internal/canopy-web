// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import type { AiStatusLegacy } from '@/api/ai'
import type { PresencePreferenceOut } from '@/api/presence'
import type { MintDebugSessionResponse } from '@/api/debug'

// NOTE ON CONVENTION: canopy-web has no @testing-library/jest-dom and no
// user-event package. Assertions use toBeTruthy()/toBe(), interactions use
// fireEvent, and every DOM test carries the `@vitest-environment jsdom`
// docblock above — the vitest config sets no global environment.
//
// vi.mock is hoisted above these declarations by vitest's transform, but the
// factory below isn't *called* until the mocked modules are actually
// imported — which happens on the dynamic imports below, well after these
// consts are assigned. Same pattern as RunnerAssignments.test.tsx.

const aiStatus = vi.fn<() => Promise<AiStatusLegacy>>()
const aiSwitch = vi.fn()
const aiAuthStart = vi.fn()
const aiAuthComplete = vi.fn()
const aiAuthPoll = vi.fn()
vi.mock('@/api/ai', () => ({ aiStatus, aiSwitch, aiAuthStart, aiAuthComplete, aiAuthPoll }))

const mintDebugSession = vi.fn<(ttl?: number) => Promise<MintDebugSessionResponse>>()
vi.mock('@/api/debug', () => ({ mintDebugSession }))

const getPresencePreference = vi.fn<() => Promise<PresencePreferenceOut>>()
const setPresencePreference = vi.fn<(next: boolean) => Promise<PresencePreferenceOut>>()
vi.mock('@/api/presence', () => ({ getPresencePreference, setPresencePreference }))

const { SettingsPage } = await import('./SettingsPage')
const { PRESENCE_PREFERENCE_CHANGED_EVENT } = await import('@/presence/events')
const { usePresenceReconnectNonce } = await import('@/presence/usePresenceReconnectNonce')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SettingsPage presence toggle', () => {
  it('dispatches the presence-reconnect signal after a successful PATCH, and the app shell bumps its remount key in response', async () => {
    aiStatus.mockResolvedValue({ backend: 'api', ready: true, detail: 'ok', setup_hint: null })
    getPresencePreference.mockResolvedValue({ show_presence: true })
    setPresencePreference.mockResolvedValue({ show_presence: false })

    // Stand-in for AppLayout's remount key — this is the SAME hook AppLayout
    // uses (`<PresenceHeaderBadge key={usePresenceReconnectNonce()} />`),
    // rendered independently so this test doesn't need to mount the whole
    // shell (WorkspaceProvider/AuthProvider/router context) to observe it.
    const shell = renderHook(() => usePresenceReconnectNonce())
    expect(shell.result.current).toBe(0)

    render(<SettingsPage />)

    const checkbox = await screen.findByRole('checkbox', { name: /show me as viewing/i })
    expect((checkbox as HTMLInputElement).checked).toBe(true)

    await act(async () => {
      fireEvent.click(checkbox)
      // Let the optimistic setState + the PATCH promise + the dispatched
      // event's listeners all flush.
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(setPresencePreference).toHaveBeenCalledWith(false)
    expect((checkbox as HTMLInputElement).checked).toBe(false)
    // The event actually fired (same-tab path)...
    expect(shell.result.current).toBe(1)
  })

  it('does not dispatch the signal, and reverts the checkbox, if the PATCH fails', async () => {
    aiStatus.mockResolvedValue({ backend: 'api', ready: true, detail: 'ok', setup_hint: null })
    getPresencePreference.mockResolvedValue({ show_presence: true })
    setPresencePreference.mockRejectedValue(new Error('network error'))

    const handler = vi.fn()
    window.addEventListener(PRESENCE_PREFERENCE_CHANGED_EVENT, handler)

    render(<SettingsPage />)
    const checkbox = await screen.findByRole('checkbox', { name: /show me as viewing/i })

    await act(async () => {
      fireEvent.click(checkbox)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(handler).not.toHaveBeenCalled()
    expect((checkbox as HTMLInputElement).checked).toBe(true) // reverted

    window.removeEventListener(PRESENCE_PREFERENCE_CHANGED_EVENT, handler)
  })

  it("the checkbox's accessible name is just the label, not the help sentence", async () => {
    aiStatus.mockResolvedValue({ backend: 'api', ready: true, detail: 'ok', setup_hint: null })
    getPresencePreference.mockResolvedValue({ show_presence: true })

    render(<SettingsPage />)

    // Resolves only if the accessible name is exactly this — a leaked help
    // sentence in the name (the a11y bug this fixes) would make this throw.
    const checkbox = await screen.findByRole('checkbox', { name: 'Show me as viewing' })
    expect(checkbox.getAttribute('aria-describedby')).toBe('presence-toggle-help')
    expect(screen.getByText(/they cannot see you/i)).toBeTruthy()
  })
})
