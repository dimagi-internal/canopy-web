// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { PersonalToken } from '@/api/tokens'

// vi.mock is hoisted above these declarations by vitest's transform, but the
// factory isn't *called* until the mocked modules are actually imported —
// which happens on the dynamic import below. Same pattern as
// SettingsPage.presence.test.tsx.
const listTokens = vi.fn<() => Promise<PersonalToken[]>>()
const mintToken = vi.fn()
const revokeToken = vi.fn()
vi.mock('@/api/tokens', () => ({ listTokens, mintToken, revokeToken }))

const { TokensPanel } = await import('./TokensPanel')

const TOKEN: PersonalToken = {
  id: 1,
  label: 'my laptop',
  created_at: '2026-09-01T00:00:00Z',
  last_used_at: null,
  revoked_at: null,
  expires_at: null,
} as PersonalToken

describe('TokensPanel — revoke confirm gate', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('does NOT call revokeToken when window.confirm is declined', async () => {
    listTokens.mockResolvedValue([TOKEN])
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    render(<TokensPanel />)
    const revokeBtn = await screen.findByText('Revoke')

    await act(async () => {
      fireEvent.click(revokeBtn)
      await Promise.resolve()
    })

    expect(confirmSpy).toHaveBeenCalled()
    expect(revokeToken).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('DOES call revokeToken when window.confirm is accepted', async () => {
    listTokens.mockResolvedValue([TOKEN])
    revokeToken.mockResolvedValue(true)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    render(<TokensPanel />)
    const revokeBtn = await screen.findByText('Revoke')

    await act(async () => {
      fireEvent.click(revokeBtn)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(revokeToken).toHaveBeenCalledWith(TOKEN.id)
    confirmSpy.mockRestore()
  })
})
