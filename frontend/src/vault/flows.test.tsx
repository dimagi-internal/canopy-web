// @vitest-environment jsdom
// The zero-knowledge vault flows through their UI (canopy-web#1550), with the
// REAL crypto module and WebAuthn mocked at its one seam (`assertWithPrf`,
// which `prf.test.ts` covers against navigator.credentials). Each test opens
// what the UI sent with the keys only the person/device holds — proving the
// payloads are the protocol, not just that a call was made.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  getKeyset: vi.fn(), putKeyset: vi.fn(), getZkState: vi.fn(), enableZk: vi.fn(), addZkWraps: vi.fn(),
  putZkValues: vi.fn(), rotateZk: vi.fn(), disableZk: vi.fn(),
}))
vi.mock('@/api/vault', () => api)
const { revealAgentCredential } = vi.hoisted(() => ({ revealAgentCredential: vi.fn() }))
vi.mock('@/api/agents', () => ({ revealAgentCredential }))
const prf = vi.hoisted(() => ({ assertWithPrf: vi.fn(), browserSupportsPrf: vi.fn() }))
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('./prf')>()), ...prf }))

const zk = await import('./zk')
const { VaultKeyPanel } = await import('@/components/settings/VaultKeyPanel')
const { AgentZkSection, ZK_TRUST_CAVEAT } = await import('@/pages/agents/AgentZkSection')
const { PRF_UNSUPPORTED } = await import('./prf')

const USER = '7'
const AGENT_ID = '42'
const PRF_OUT = new Uint8Array(32).fill(9)

function keysetOut(over: Record<string, unknown> = {}) {
  return { user_id: USER, prf_input: 'cHJm', exists: false, public_jwk: null, seals: [],
           passkeys: [{ credential_id: 'cred-1', label: 'mac', prf_enabled: true, sealed: false }], ...over }
}

function memoryStorage(): Storage {
  const m = new Map<string, string>()
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, String(v)),
           removeItem: (k) => void m.delete(k), clear: () => m.clear(), key: (i) => [...m.keys()][i] ?? null,
           get length() { return m.size } }
}

beforeEach(() => {
  prf.browserSupportsPrf.mockResolvedValue(true)
  Object.defineProperty(window, 'localStorage', { value: memoryStorage(), configurable: true })
  // A WebAuthn-capable browser; the ceremony itself is `assertWithPrf`, mocked.
  vi.stubGlobal('PublicKeyCredential', function PublicKeyCredential() {})
  Object.defineProperty(navigator, 'credentials', { value: {}, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('Set up vault key', () => {
  it('with a passkey: seals a fresh key under the PRF output', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: PRF_OUT })
    api.putKeyset.mockImplementation(async (b) => keysetOut({ exists: true, ...b }))
    render(<VaultKeyPanel />)
    fireEvent.click(await screen.findByTestId('vault-key-setup'))
    await waitFor(() => expect(api.putKeyset).toHaveBeenCalled())
    const body = api.putKeyset.mock.calls[0][0]
    expect(body.seals[0].alg).toBe('prf')
    // The seal opens with the PRF output to a scalar that IS the public key's.
    const scalar = await zk.openUserKey(body.seals[0], USER, PRF_OUT)
    const priv = await zk.importPrivateScalar(scalar, body.public_jwk)
    const wrap = await zk.wrapEcdhEs(new Uint8Array(32).fill(1), body.public_jwk, 'i')
    expect(Array.from(await zk.unwrapEcdhEs(wrap, priv, body.public_jwk, 'i'))).toEqual(Array(32).fill(1))
  })

  it('says plainly when the passkey gives no PRF output', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: null })
    render(<VaultKeyPanel />)
    fireEvent.click(await screen.findByTestId('vault-key-setup'))
    expect((await screen.findByTestId('vault-key-error')).textContent).toBe(PRF_UNSUPPORTED)
    expect(api.putKeyset).not.toHaveBeenCalled()
  })

  it('offers the passphrase when the browser lacks PRF', async () => {
    prf.browserSupportsPrf.mockResolvedValue(false)
    api.getKeyset.mockResolvedValue(keysetOut())
    render(<VaultKeyPanel />)
    expect(await screen.findByTestId('vault-key-unsupported')).toBeTruthy()
    await waitFor(() => expect((screen.getByTestId('vault-key-setup') as HTMLButtonElement).disabled).toBe(true))
    expect((screen.getByTestId('vault-key-setup-passphrase') as HTMLButtonElement).disabled).toBe(false)
  })

  it('with a passphrase: enforces strength, seals with a Secret Key shown once and remembered here', async () => {
    api.getKeyset.mockResolvedValue(keysetOut({ passkeys: [] }))
    api.putKeyset.mockImplementation(async (b) => keysetOut({ exists: true, passkeys: [], ...b }))
    render(<VaultKeyPanel />)
    fireEvent.click(await screen.findByTestId('vault-key-setup-passphrase'))
    const [p1, p2] = [screen.getByLabelText('New vault passphrase'), screen.getByLabelText('Repeat the passphrase')]
    fireEvent.change(p1, { target: { value: 'short' } })
    expect(screen.getByTestId('vault-passphrase-problem')).toBeTruthy()
    fireEvent.change(p1, { target: { value: 'orchid lantern 42 vapor' } })
    fireEvent.change(p2, { target: { value: 'orchid lantern 42 vapor' } })
    fireEvent.click(screen.getByTestId('vault-passphrase-save'))
    const code = (await screen.findByTestId('vault-secret-key', {}, { timeout: 5000 })).textContent!
    expect(code).toMatch(/^CV1-/)
    expect(window.localStorage.getItem(`canopy-vault-secret-key:${USER}`)).toBe(code)
    const body = api.putKeyset.mock.calls[0][0]
    expect(body.seals[0]).toMatchObject({ alg: '2skd', kdf: 'pbkdf2-sha256', i: 650000 })
    expect(JSON.stringify(body)).not.toContain('orchid')
    const scalar = await zk.openUserKey2skd(body.seals[0], USER, 'orchid lantern 42 vapor', zk.parseSecretKeyCode(code))
    expect(scalar.length).toBe(32)
    expect(screen.getByTestId('vault-emergency-kit')).toBeTruthy()
  })
})

// ---- the agent section -------------------------------------------------------------------------

async function person() {
  return zk.generateKeyPair()
}

describe('Turn on zero-knowledge', () => {
  it('encrypts every value, wraps for me, the device and recovery, and shows the code before enabling', async () => {
    const me = await person()
    const device = await person()
    const deviceJkt = await zk.jkt(device.publicJwk)
    api.getKeyset.mockResolvedValue(keysetOut({ exists: true, public_jwk: me.publicJwk }))
    api.getZkState.mockResolvedValue({ agent: 'eva', agent_id: AGENT_ID, enabled: false, kv: 0, wraps: [], values: null,
      rotation_recommended: false,
      pending: [{ kind: 'device', target: 'dk1', public_jwk: device.publicJwk, ecdh_jkt: deviceJkt,
                  runner_name: 'box', label: 'mbp', device_kind: 'secure-enclave' }] })
    revealAgentCredential.mockImplementation(async (_s: string, n: string) => `${n}-plain`)
    api.enableZk.mockImplementation(async () => ({ agent: 'eva', agent_id: AGENT_ID, enabled: true, kv: 1,
      wraps: [], pending: [], rotation_recommended: false, values: null }))
    render(<AgentZkSection slug="eva" storedNames={['canopy-pat']} canEdit />)
    expect((await screen.findByTestId('zk-trust-caveat')).textContent).toBe(ZK_TRUST_CAVEAT)
    fireEvent.click(await screen.findByTestId('zk-enable'))
    const dialog = await screen.findByTestId('zk-recovery-code')
    const code = dialog.querySelector('code')!.textContent!
    expect(api.enableZk).not.toHaveBeenCalled()  // not before the code is kept
    fireEvent.click(screen.getByTestId('zk-code-saved'))
    fireEvent.click(screen.getByTestId('zk-code-continue'))
    await waitFor(() => expect(api.enableZk).toHaveBeenCalled())
    const body = api.enableZk.mock.calls[0][1]
    expect(body.kv).toBe(1)
    expect(body.wraps.map((w: { kind: string }) => w.kind).sort()).toEqual(['device', 'member', 'recovery'])
    const rec = body.wraps.find((w: { kind: string }) => w.kind === 'recovery').wrap
    const vk = await zk.unwrapRecovery(rec, AGENT_ID, 1, zk.parseRecoveryCode(code))
    expect(await zk.decryptValue(vk, AGENT_ID, 'canopy-pat', 1, body.values[0].value_zk)).toBe('canopy-pat-plain')
    const dw = body.wraps.find((w: { kind: string }) => w.kind === 'device')
    expect(dw.target).toBe('dk1')
    expect(Array.from(await zk.unwrapEcdhEs(dw.wrap, device.privateKey, device.publicJwk,
      zk.deviceInfo(AGENT_ID, 1, deviceJkt)))).toEqual(Array.from(vk))
    const mw = body.wraps.find((w: { kind: string }) => w.kind === 'member')
    expect(Array.from(await zk.unwrapEcdhEs(mw.wrap, me.privateKey, me.publicJwk,
      zk.memberInfo(AGENT_ID, 1, USER)))).toEqual(Array.from(vk))
  })

  it('cancelling the recovery code changes nothing', async () => {
    const me = await person()
    api.getKeyset.mockResolvedValue(keysetOut({ exists: true, public_jwk: me.publicJwk }))
    api.getZkState.mockResolvedValue({ agent: 'eva', agent_id: AGENT_ID, enabled: false, kv: 0, wraps: [],
      pending: [], rotation_recommended: false, values: null })
    render(<AgentZkSection slug="eva" storedNames={[]} canEdit />)
    fireEvent.click(await screen.findByTestId('zk-enable'))
    await screen.findByTestId('zk-recovery-code')
    fireEvent.click(screen.getByText('Cancel'))
    expect((await screen.findByTestId('zk-error')).textContent).toMatch(/nothing was changed/)
    expect(api.enableZk).not.toHaveBeenCalled()
  })

  it('asks for a vault key first when the person has none', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    api.getZkState.mockResolvedValue({ agent: 'eva', agent_id: AGENT_ID, enabled: false, kv: 0, wraps: [],
      pending: [], rotation_recommended: false, values: null })
    render(<AgentZkSection slug="eva" storedNames={[]} canEdit />)
    expect(await screen.findByTestId('zk-needs-vault-key')).toBeTruthy()
  })
})

describe('Grant pending, unlocking with a passphrase', () => {
  it('opens VK with my passphrase-sealed key and wraps it to the pending device', async () => {
    const me = await person()
    const device = await person()
    const deviceJkt = await zk.jkt(device.publicJwk)
    const vk = zk.randomBytes(32)
    const sk = zk.randomBytes(16)
    window.localStorage.setItem(`canopy-vault-secret-key:${USER}`, zk.secretKeyCode(sk))
    const seal = await zk.sealUserKey2skd(me.scalar, USER, 'a long enough passphrase', sk)
    api.getKeyset.mockResolvedValue(keysetOut({ exists: true, public_jwk: me.publicJwk, seals: [seal], passkeys: [] }))
    const myWrap = await zk.wrapEcdhEs(vk, me.publicJwk, zk.memberInfo(AGENT_ID, 1, USER))
    const state = { agent: 'eva', agent_id: AGENT_ID, enabled: true, kv: 1, rotation_recommended: false, values: null,
      wraps: [{ kind: 'member', kv: 1, target: USER, label: 'me', current: true, wrap: myWrap, created_at: '' }],
      pending: [{ kind: 'device', target: 'dk1', public_jwk: device.publicJwk, ecdh_jkt: deviceJkt, runner_name: 'box' }] }
    api.getZkState.mockResolvedValue(state)
    api.addZkWraps.mockResolvedValue({ ...state, pending: [] })
    render(<AgentZkSection slug="eva" storedNames={[]} canEdit />)
    fireEvent.click(await screen.findByTestId('zk-grant'))
    await screen.findByTestId('vault-unlock')
    expect(screen.queryByLabelText('Secret Key')).toBeNull()  // remembered in this browser
    expect(screen.queryByTestId('vault-unlock-passkey')).toBeNull()
    fireEvent.change(screen.getByLabelText('Vault passphrase'), { target: { value: 'a long enough passphrase' } })
    fireEvent.click(screen.getByTestId('vault-unlock-passphrase'))
    await waitFor(() => expect(api.addZkWraps).toHaveBeenCalled(), { timeout: 5000 })
    const { kv, wraps } = api.addZkWraps.mock.calls[0][1]
    expect(kv).toBe(1)
    expect(wraps[0]).toMatchObject({ kind: 'device', target: 'dk1' })
    expect(Array.from(await zk.unwrapEcdhEs(wraps[0].wrap, device.privateKey, device.publicJwk,
      zk.deviceInfo(AGENT_ID, 1, deviceJkt)))).toEqual(Array.from(vk))
  })
})

describe('Vault key card on Settings', () => {
  it('stays hidden until a person has a key or was sent to set one up', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    window.location.hash = ''
    const { container } = render(<VaultKeyPanel onlyWhenNeeded />)
    await waitFor(() => expect(api.getKeyset).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="settings-vault-key"]')).toBeNull()
  })

  it('shows when linked as #vault-key', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    window.location.hash = '#vault-key'
    render(<VaultKeyPanel onlyWhenNeeded />)
    expect(await screen.findByTestId('settings-vault-key')).toBeTruthy()
    window.location.hash = ''
  })

  it('shows once a key exists', async () => {
    api.getKeyset.mockResolvedValue(keysetOut({ exists: true, public_jwk: { kty: 'EC' }, seals: [{ alg: 'prf' }] }))
    render(<VaultKeyPanel onlyWhenNeeded />)
    expect(await screen.findByTestId('settings-vault-key')).toBeTruthy()
  })
})
