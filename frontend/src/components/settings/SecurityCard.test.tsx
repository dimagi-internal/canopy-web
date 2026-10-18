// @vitest-environment jsdom
// The Security card (canopy-web#1550): one card with You / Your machines /
// Your agents, and the person's ONE recovery code — made once, used to get back in.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const vault = vi.hoisted(() => ({ getKeyset: vi.fn(), putKeyset: vi.fn(), getZkState: vi.fn() }))
vi.mock('@/api/vault', () => vault)
const passkeys = vi.hoisted(() => ({ listPasskeys: vi.fn(async () => []), registerThisDevice: vi.fn(), deletePasskey: vi.fn() }))
vi.mock('@/api/passkeys', () => passkeys)
vi.mock('@/api/agents', () => ({ listAgents: vi.fn(async () => ({ items: [] })), getAgentCredentialStatus: vi.fn() }))
const prf = vi.hoisted(() => ({ assertWithPrf: vi.fn(), browserSupportsPrf: vi.fn(async () => true) }))
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('@/vault/prf')>()), ...prf }))

const zk = await import('@/vault/zk')
const { SecurityCard } = await import('./SecurityCard')

const USER = '7'
const PRF_OUT = new Uint8Array(32).fill(9)

beforeEach(() => {
  vi.stubGlobal('PublicKeyCredential', function PublicKeyCredential() {})
  Object.defineProperty(navigator, 'credentials', { value: {}, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  window.location.hash = ''
})

async function keyset(extra: unknown[] = []) {
  const me = await zk.generateKeyPair()
  const seal = await zk.sealUserKey(me.scalar, USER, 'cred-1', PRF_OUT)
  const ks = { user_id: USER, prf_input: 'cHJm', exists: true, public_jwk: me.publicJwk, seals: [seal, ...extra],
               passkeys: [{ credential_id: 'cred-1', label: 'mac', prf_enabled: true, sealed: true }] }
  vault.getKeyset.mockResolvedValue(ks)
  vault.putKeyset.mockImplementation(async (b: Record<string, unknown>) => {
    const next = { ...ks, ...b }
    vault.getKeyset.mockResolvedValue(next)
    return next
  })
  prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: PRF_OUT })
  return me
}

describe('SecurityCard', () => {
  it('is one card with You, Your machines and Your agents', async () => {
    vault.getKeyset.mockResolvedValue({ user_id: USER, exists: false, passkeys: [], seals: [] })
    render(<SecurityCard machines={<p>machines here</p>} />)
    expect(await screen.findByTestId('security-you')).toBeTruthy()
    expect(screen.getByTestId('security-your-machines').textContent).toContain('machines here')
    expect(screen.getByTestId('security-your-agents')).toBeTruthy()
    expect(await screen.findByTestId('security-no-key')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/PRF|ECDH|wrap|\bkv\b/)
  })

  it('set up once, from the You row: one passkey touch makes the key, then the recovery code — no agent involved', async () => {
    const base = { user_id: USER, prf_input: 'cHJm', exists: false, public_jwk: null, seals: [],
                   passkeys: [{ credential_id: 'cred-1', label: 'mac', prf_enabled: true, sealed: false }] }
    vault.getKeyset.mockResolvedValue(base)
    vault.putKeyset.mockImplementation(async (b: Record<string, unknown>) => {
      const next = { ...base, exists: true, ...b }
      vault.getKeyset.mockResolvedValue(next)
      return next
    })
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: PRF_OUT })
    render(<SecurityCard machines={null} />)
    fireEvent.click(await screen.findByTestId('security-setup'))
    await screen.findByTestId('security-recovery-code')
    expect(prf.assertWithPrf).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('security-recovery-saved'))
    fireEvent.click(screen.getByTestId('security-recovery-keep'))
    expect(await screen.findByTestId('security-recovery-ok')).toBeTruthy()
    const last = vault.putKeyset.mock.calls.at(-1)![0]
    expect(last.seals.map((s: { alg: string }) => s.alg)).toEqual(['prf', 'recovery'])
  })

  it('makes the ONE recovery code once, and stores it only after it is kept', async () => {
    const me = await keyset()
    render(<SecurityCard machines={null} />)
    fireEvent.click(await screen.findByTestId('security-recovery-create'))
    const code = (await screen.findByTestId('security-recovery-code')).querySelector('code')!.textContent!
    expect(vault.putKeyset).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('security-recovery-saved'))
    fireEvent.click(screen.getByTestId('security-recovery-keep'))
    expect(await screen.findByTestId('security-recovery-ok')).toBeTruthy()
    const seals = vault.putKeyset.mock.calls[0][0].seals
    expect(seals.map((s: { alg: string }) => s.alg)).toEqual(['prf', 'recovery'])
    expect(Array.from(await zk.openUserKeyRecovery(seals[1], USER, zk.parseRecoveryCode(code)))).toEqual(Array.from(me.scalar))
  })

  it('lost your passkey: the code opens the key and a new passkey holds it', async () => {
    const me = await zk.generateKeyPair()
    const r = zk.randomBytes(32)
    const rseal = await zk.sealUserKeyRecovery(me.scalar, USER, r)
    const ks = { user_id: USER, prf_input: 'cHJm', exists: true, public_jwk: me.publicJwk, seals: [rseal],
                 passkeys: [] }
    vault.getKeyset.mockResolvedValue(ks)
    vault.putKeyset.mockImplementation(async (b: Record<string, unknown>) => ({ ...ks, ...b }))
    passkeys.registerThisDevice.mockResolvedValue({ credential_id: 'fresh', prf_enabled: true })
    prf.assertWithPrf.mockResolvedValue({ cred: 'fresh', prf: PRF_OUT })
    render(<SecurityCard machines={null} />)
    fireEvent.click(await screen.findByTestId('security-recover'))
    fireEvent.change(screen.getByLabelText('Recovery code'), { target: { value: zk.recoveryCode(r) } })
    fireEvent.click(screen.getByTestId('security-recover-passkey'))
    await waitFor(() => expect(vault.putKeyset).toHaveBeenCalled())
    const seals = vault.putKeyset.mock.calls[0][0].seals
    const fresh = seals.find((s: { alg: string }) => s.alg === 'prf')
    expect(Array.from(await zk.openUserKey(fresh, USER, PRF_OUT))).toEqual(Array.from(me.scalar))
    expect(seals.some((s: { alg: string }) => s.alg === 'recovery')).toBe(true)  // the code still works
  })

  it('a wrong code changes nothing', async () => {
    const me = await zk.generateKeyPair()
    const rseal = await zk.sealUserKeyRecovery(me.scalar, USER, zk.randomBytes(32))
    vault.getKeyset.mockResolvedValue({ user_id: USER, prf_input: 'cHJm', exists: true, public_jwk: me.publicJwk,
                                        seals: [rseal], passkeys: [] })
    passkeys.registerThisDevice.mockResolvedValue({ credential_id: 'fresh', prf_enabled: true })
    render(<SecurityCard machines={null} />)
    fireEvent.click(await screen.findByTestId('security-recover'))
    fireEvent.change(screen.getByLabelText('Recovery code'), { target: { value: zk.recoveryCode(zk.randomBytes(32)) } })
    fireEvent.click(screen.getByTestId('security-recover-passkey'))
    expect((await screen.findByTestId('security-error')).textContent).toMatch(/does not open/)
    expect(vault.putKeyset).not.toHaveBeenCalled()
  })

  it('checks a saved recovery code in the browser and changes nothing', async () => {
    const me = await zk.generateKeyPair()
    const r = zk.randomBytes(32)
    const rseal = await zk.sealUserKeyRecovery(me.scalar, USER, r)
    vault.getKeyset.mockResolvedValue({ user_id: USER, prf_input: 'cHJm', exists: true, public_jwk: me.publicJwk,
                                        seals: [rseal], passkeys: [] })
    render(<SecurityCard machines={null} />)
    fireEvent.click(await screen.findByTestId('security-check'))
    const box = screen.getByLabelText('Recovery code to check')

    fireEvent.change(box, { target: { value: zk.recoveryCode(r).toLowerCase() } })
    fireEvent.click(screen.getByTestId('security-check-go'))
    expect((await screen.findByTestId('security-check-result')).textContent).toBe('This code works ✓')

    fireEvent.change(box, { target: { value: zk.recoveryCode(zk.randomBytes(32)) } })
    fireEvent.click(screen.getByTestId('security-check-go'))
    expect((await screen.findByTestId('security-check-result')).textContent).toMatch(/doesn’t match/)

    fireEvent.change(box, { target: { value: 'ABCD-EFGH' } })
    fireEvent.click(screen.getByTestId('security-check-go'))
    expect((await screen.findByTestId('security-check-result')).textContent).toMatch(/doesn’t look like a recovery code/)

    expect(vault.putKeyset).not.toHaveBeenCalled()
    expect(passkeys.registerThisDevice).not.toHaveBeenCalled()
    expect(prf.assertWithPrf).not.toHaveBeenCalled()
  })

  it('#vault-key opens the extra options', async () => {
    await keyset()
    window.location.hash = '#vault-key'
    render(<SecurityCard machines={null} />)
    expect(await screen.findByTestId('settings-vault-key')).toBeTruthy()
  })
})
