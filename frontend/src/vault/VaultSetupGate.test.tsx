// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const getKeyset = vi.fn()
const registerThisDevice = vi.fn(async () => ({ credential_id: 'c1', prf_enabled: false }))
vi.mock('@/api/vault', () => ({ getKeyset, getVaultSetup: vi.fn() }))
vi.mock('@/api/passkeys', () => ({ registerThisDevice }))
vi.mock('@/auth/webauthn', () => ({ webauthnSupported: () => true, defaultLabel: () => 'Mac (Touch ID)' }))

const { VaultSetupScreen } = await import('./VaultSetupGate')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('VaultSetupScreen', () => {
  it('registers a passkey before a passphrase key, since saving one needs a passkey confirmation', async () => {
    getKeyset.mockResolvedValue({ passkeys: [] })
    render(<VaultSetupScreen hasPasskey={false} onDone={() => {}} />)

    fireEvent.click(screen.getByTestId('vault-setup-passphrase'))

    await waitFor(() => expect(screen.getByTestId('vault-passphrase-setup')).toBeTruthy())
    expect(registerThisDevice).toHaveBeenCalledWith('Mac (Touch ID)')
  })

  it('goes straight to the passphrase when a passkey exists', async () => {
    render(<VaultSetupScreen hasPasskey onDone={() => {}} />)

    fireEvent.click(screen.getByTestId('vault-setup-passphrase'))

    await waitFor(() => expect(screen.getByTestId('vault-passphrase-setup')).toBeTruthy())
    expect(registerThisDevice).not.toHaveBeenCalled()
  })
})
