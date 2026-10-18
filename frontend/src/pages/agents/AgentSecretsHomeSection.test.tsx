// @vitest-environment jsdom
// Where a new agent's secrets live and its one-click canopy login — no terminal,
// no 1Password (marshellis audit, 2026-10-18).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const dk = vi.hoisted(() => ({ getPatRotation: vi.fn(), giveCanopyLogin: vi.fn(), setCredentialSource: vi.fn() }))
vi.mock('@/api/deviceKeys', () => dk)
const vault = vi.hoisted(() => ({ getKeyset: vi.fn() }))
vi.mock('@/api/vault', () => vault)
const ops = vi.hoisted(() => ({ vaultKeyForFlow: vi.fn(), prepareRecoveryCode: vi.fn() }))
vi.mock('@/vault/operations', () => ops)
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('@/vault/prf')>()), browserSupportsPrf: vi.fn(async () => true) }))

const { AgentSecretsHomeSection } = await import('./AgentSecretsHomeSection')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentSecretsHomeSection', () => {
  it('a new agent is on the Canopy vault and offers its login in one click', async () => {
    dk.getPatRotation.mockResolvedValue({ enabled: false, period_days: 30, history: [] })
    vault.getKeyset.mockResolvedValue({ exists: true })
    dk.giveCanopyLogin.mockResolvedValue({ user_email: 'scout@marshellis.com', created_user: true, token_id: 1, rotation_enabled: true })
    const onChanged = vi.fn()
    render(<AgentSecretsHomeSection slug="scout" credentialSource="canopy-web" storedNames={[]} canEdit onChanged={onChanged} />)
    expect(screen.getByTestId('secrets-home-canopy-web').getAttribute('aria-pressed')).toBe('true')
    expect(screen.queryByText(/1password vault|op inject|canopy agent vault/i)).toBeNull()
    fireEvent.click(screen.getByTestId('canopy-login-give'))
    expect((await screen.findByTestId('canopy-login-note')).textContent).toMatch(/scout@marshellis.com.*renews itself/)
    expect(dk.giveCanopyLogin).toHaveBeenCalledWith('scout')
    expect(onChanged).toHaveBeenCalled()
  })

  it('someone with no key sets it up first — recovery code kept before the login is made', async () => {
    dk.getPatRotation.mockResolvedValue({ enabled: false, period_days: 30, history: [] })
    vault.getKeyset.mockResolvedValue({ exists: false })
    ops.vaultKeyForFlow.mockResolvedValue({ me: { userId: '1', scalar: new Uint8Array(32) }, replaced: [] })
    const commit = vi.fn(async () => ({}))
    ops.prepareRecoveryCode.mockResolvedValue({ code: 'CODE-1234', commit })
    dk.giveCanopyLogin.mockResolvedValue({ user_email: 'scout@marshellis.com', created_user: true, token_id: 1, rotation_enabled: true })
    render(<AgentSecretsHomeSection slug="scout" credentialSource="canopy-web" storedNames={[]} canEdit />)
    fireEvent.click(screen.getByTestId('canopy-login-give'))
    expect((await screen.findByTestId('canopy-login-code')).textContent).toContain('CODE-1234')
    expect(dk.giveCanopyLogin).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('canopy-login-saved'))
    fireEvent.click(screen.getByTestId('canopy-login-continue'))
    await waitFor(() => expect(dk.giveCanopyLogin).toHaveBeenCalledWith('scout'))
    expect(commit).toHaveBeenCalled()
  })

  it('shows a stored, renewing login as done', async () => {
    dk.getPatRotation.mockResolvedValue({ enabled: true, period_days: 30, history: [] })
    render(<AgentSecretsHomeSection slug="scout" credentialSource="canopy-web" storedNames={['canopy-pat']} canEdit />)
    expect((await screen.findByTestId('canopy-login-ok')).textContent).toMatch(/Stored ✓.*renews itself every 30 days/)
    await waitFor(() => expect(screen.queryByTestId('canopy-login-give')).toBeNull())
  })

  it('switches to 1Password and tells the page', async () => {
    dk.getPatRotation.mockResolvedValue({ enabled: false, period_days: 30, history: [] })
    dk.setCredentialSource.mockResolvedValue(undefined)
    const onSource = vi.fn()
    render(<AgentSecretsHomeSection slug="scout" credentialSource="canopy-web" storedNames={[]} canEdit onSource={onSource} />)
    fireEvent.click(screen.getByTestId('secrets-home-1password'))
    await waitFor(() => expect(onSource).toHaveBeenCalledWith('1password'))
    expect(dk.setCredentialSource).toHaveBeenCalledWith('scout', '1password')
    expect(screen.queryByTestId('canopy-login-give')).toBeNull()
  })
})
