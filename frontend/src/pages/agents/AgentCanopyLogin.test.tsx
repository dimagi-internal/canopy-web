// @vitest-environment jsdom
// An agent's own canopy login: one click makes it (no terminal), it renews itself, and one
// button replaces it now.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const dk = vi.hoisted(() => ({
  getPatRotation: vi.fn(), giveCanopyLogin: vi.fn(), setPatRotation: vi.fn(), rotatePatNow: vi.fn(),
}))
vi.mock('@/api/deviceKeys', () => dk)
const agents = vi.hoisted(() => ({ getAgentCredentialStatus: vi.fn() }))
vi.mock('@/api/agents', () => agents)
const vault = vi.hoisted(() => ({ getKeyset: vi.fn() }))
vi.mock('@/api/vault', () => vault)
const ops = vi.hoisted(() => ({ vaultKeyForFlow: vi.fn(), prepareRecoveryCode: vi.fn() }))
vi.mock('@/vault/operations', () => ops)
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('@/vault/prf')>()), browserSupportsPrf: vi.fn(async () => true) }))

const { AgentCanopyLogin } = await import('./AgentCanopyLogin')

const ROTATION = { enabled: true, period_days: 30, last_rotated_at: null, next_due_at: null, enabled_by_email: '', history: [] }
const stored = (set: boolean) => agents.getAgentCredentialStatus.mockResolvedValue(
  set ? [{ name: 'CANOPY_WEB_PAT', set: true }] : [])

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentCanopyLogin', () => {
  it('offers its login in one click when it has none', async () => {
    stored(false)
    dk.getPatRotation.mockResolvedValue({ ...ROTATION, enabled: false })
    vault.getKeyset.mockResolvedValue({ exists: true })
    dk.giveCanopyLogin.mockResolvedValue({ user_email: 'scout@marshellis.com', created_user: true, token_id: 1, rotation_enabled: true })
    const onChanged = vi.fn()
    render(<AgentCanopyLogin slug="scout" canEdit onChanged={onChanged} />)
    fireEvent.click(await screen.findByTestId('canopy-login-give'))
    expect((await screen.findByTestId('canopy-login-note')).textContent).toMatch(/scout@marshellis.com.*renews itself/)
    expect(dk.giveCanopyLogin).toHaveBeenCalledWith('scout')
    expect(onChanged).toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByTestId('canopy-login-give')).toBeNull())
  })

  it('someone with no key sets it up first — recovery code kept before the login is made', async () => {
    stored(false)
    dk.getPatRotation.mockResolvedValue({ ...ROTATION, enabled: false })
    vault.getKeyset.mockResolvedValue({ exists: false })
    ops.vaultKeyForFlow.mockResolvedValue({ me: { userId: '1', scalar: new Uint8Array(32) }, replaced: [] })
    const commit = vi.fn(async () => ({}))
    ops.prepareRecoveryCode.mockResolvedValue({ code: 'CODE-1234', commit })
    dk.giveCanopyLogin.mockResolvedValue({ user_email: 'scout@marshellis.com', created_user: true, token_id: 1, rotation_enabled: true })
    render(<AgentCanopyLogin slug="scout" canEdit />)
    fireEvent.click(await screen.findByTestId('canopy-login-give'))
    expect((await screen.findByTestId('canopy-login-code')).textContent).toContain('CODE-1234')
    expect(dk.giveCanopyLogin).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('canopy-login-saved'))
    fireEvent.click(screen.getByTestId('canopy-login-continue'))
    await waitFor(() => expect(dk.giveCanopyLogin).toHaveBeenCalledWith('scout'))
    expect(commit).toHaveBeenCalled()
  })

  it('shows a stored login with its renewal, and turns renewal off', async () => {
    stored(true)
    dk.getPatRotation.mockResolvedValue(ROTATION)
    dk.setPatRotation.mockResolvedValue({ ...ROTATION, enabled: false })
    render(<AgentCanopyLogin slug="scout" canEdit />)
    expect((await screen.findByTestId('canopy-login-ok')).textContent).toContain('Stored ✓')
    expect(screen.queryByTestId('canopy-login-give')).toBeNull()
    const box = (await screen.findByTestId('pat-rotation-enabled')) as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(screen.getByText(/every 30 days/)).toBeTruthy()
    fireEvent.click(box)
    await waitFor(() => expect(dk.setPatRotation).toHaveBeenCalledWith('scout', false, 30))
    await waitFor(() => expect(screen.getByTestId('pat-rotation-summary').textContent).toContain('will stop working when it expires'))
  })

  it('makes a new login now: renews when renewal is on, makes one afresh when it is off', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    stored(true)
    dk.getPatRotation.mockResolvedValue(ROTATION)
    dk.rotatePatNow.mockResolvedValue({ ...ROTATION, last_rotated_at: '2026-10-30T00:00:00Z' })
    render(<AgentCanopyLogin slug="scout" canEdit />)
    fireEvent.click(await screen.findByTestId('pat-rotate-now'))
    await waitFor(() => expect(dk.rotatePatNow).toHaveBeenCalledWith('scout'))
    expect(dk.giveCanopyLogin).not.toHaveBeenCalled()
    cleanup()

    dk.getPatRotation.mockResolvedValue({ ...ROTATION, enabled: false })
    vault.getKeyset.mockResolvedValue({ exists: true })
    dk.giveCanopyLogin.mockResolvedValue({ user_email: 'scout@marshellis.com', created_user: false, token_id: 2, rotation_enabled: false, rotation_note: '' })
    render(<AgentCanopyLogin slug="scout" canEdit />)
    fireEvent.click(await screen.findByTestId('pat-rotate-now'))
    await waitFor(() => expect(dk.giveCanopyLogin).toHaveBeenCalledWith('scout'))
  })

  it('shows someone who cannot edit the state and none of the controls', async () => {
    stored(true)
    render(<AgentCanopyLogin slug="scout" canEdit={false} />)
    expect((await screen.findByTestId('canopy-login-ok')).textContent).toContain('Stored ✓')
    expect(screen.queryByTestId('pat-rotation-enabled')).toBeNull()
    expect(screen.queryByTestId('pat-rotate-now')).toBeNull()
    expect(dk.getPatRotation).not.toHaveBeenCalled()
  })
})
