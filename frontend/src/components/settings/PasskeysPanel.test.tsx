// @vitest-environment jsdom
// The Passkeys card (canopy-web#1538/#1550): the register form only when there
// is no passkey yet, and which passkey unlocks the vault key.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const passkeys = vi.hoisted(() => ({ listPasskeys: vi.fn(), registerThisDevice: vi.fn(), deletePasskey: vi.fn() }))
vi.mock('@/api/passkeys', () => passkeys)
const vault = vi.hoisted(() => ({ getKeyset: vi.fn() }))
vi.mock('@/api/vault', () => vault)

const { PasskeysPanel } = await import('./PasskeysPanel')

const pk = (id: number, cred: string) => ({ id, credential_id: cred, label: `key ${id}`, created_at: '2026-10-16T00:00:00Z',
  last_used_at: null, backed_up: true, prf_enabled: true })

beforeEach(() => {
  vi.stubGlobal('PublicKeyCredential', function PublicKeyCredential() {})
  Object.defineProperty(navigator, 'credentials', { value: {}, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('PasskeysPanel', () => {
  it('shows the register form when there is no passkey', async () => {
    passkeys.listPasskeys.mockResolvedValue([])
    vault.getKeyset.mockResolvedValue({ exists: false, passkeys: [], seals: [] })
    render(<PasskeysPanel />)
    expect(await screen.findByTestId('passkey-register')).toBeTruthy()
    expect(screen.queryByTestId('passkeys-vault-status')).toBeNull()
  })

  it('with passkeys: hides the form behind "Add another passkey" and marks the one that unlocks the vault', async () => {
    passkeys.listPasskeys.mockResolvedValue([pk(1, 'a'), pk(2, 'b')])
    vault.getKeyset.mockResolvedValue({ exists: true, passkeys: [{ credential_id: 'a', sealed: true }, { credential_id: 'b', sealed: false }],
      seals: [{ alg: 'prf' }] })
    render(<PasskeysPanel />)
    expect(await screen.findByTestId('passkey-unlocks-vault-1')).toBeTruthy()
    expect(screen.queryByTestId('passkey-unlocks-vault-2')).toBeNull()
    expect(screen.queryByTestId('passkey-register')).toBeNull()
    fireEvent.click(screen.getByTestId('passkey-add-another'))
    expect(screen.getByTestId('passkey-register')).toBeTruthy()
    expect(screen.queryByTestId('passkeys-vault-status')).toBeNull()  // no passphrase seal
  })
})
