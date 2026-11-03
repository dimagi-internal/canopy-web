// @vitest-environment jsdom
// The zero-knowledge vault flows through their UI (canopy-web#1550), with the
// REAL crypto module and WebAuthn mocked at its one seam (`assertWithPrf`,
// which `prf.test.ts` covers against navigator.credentials). Each test opens
// what the UI sent with the keys only the person/device holds — proving the
// payloads are the protocol, not just that a call was made.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const api = vi.hoisted(() => ({
  getKeyset: vi.fn(), putKeyset: vi.fn(), getZkState: vi.fn(), enableZk: vi.fn(), addZkWraps: vi.fn(),
  putZkValues: vi.fn(), rotateZk: vi.fn(), getVaultMembers: vi.fn(), listWorkspaceVaults: vi.fn(),
  listMyAgentsWithVaults: vi.fn(), listMyWorkspacesWithVaults: vi.fn(), reportVaultClient: vi.fn(),
  VaultApiError: class extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))
vi.mock('@/api/vault', () => api)
const passkeysApi = vi.hoisted(() => ({ registerThisDevice: vi.fn(), listPasskeys: vi.fn(), deletePasskey: vi.fn() }))
vi.mock('@/api/passkeys', () => passkeysApi)
const chat = vi.hoisted(() => ({ myKeyCopies: vi.fn(), putMyKeyCopies: vi.fn() }))
vi.mock('@/api/chat', async (orig) => ({ ...(await orig<typeof import('@/api/chat')>()), ...chat }))
const prf = vi.hoisted(() => ({ assertWithPrf: vi.fn(), browserSupportsPrf: vi.fn() }))
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('./prf')>()), ...prf }))

const zk = await import('./zk')
const { VaultKeyPanel } = await import('@/components/settings/VaultKeyPanel')
const { AgentZkSection, ZK_TRUST_CAVEAT } = await import('@/pages/agents/AgentZkSection')
const { USE_A_PASSPHRASE } = await import('./prf')
const { unlockVaultKey, vaultKeyRoute } = await import('./operations')
const { wrapInfo } = await import('./sealedChat')

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

  it('moves to the passphrase, in plain words, when the passkey gives no PRF output', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: null })
    render(<VaultKeyPanel />)
    fireEvent.click(await screen.findByTestId('vault-key-setup'))
    expect((await screen.findByTestId('vault-key-error')).textContent).toBe(USE_A_PASSPHRASE)
    expect(screen.getByTestId('vault-passphrase-form')).toBeTruthy()
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

describe('Create a new vault key', () => {
  // Someone who lost the passphrase + Secret Key: the only seal is a passphrase one, so the old key cannot open.
  const lockedOut = () => keysetOut({
    exists: true, public_jwk: { kty: 'EC', crv: 'P-256', x: 'x', y: 'y' }, seals: [{ alg: '2skd' }],
    passkeys: [{ credential_id: 'cred-1', label: 'mac', prf_enabled: true, sealed: false }] })
  const copy = { session_id: 's-1', conversation: 'c-1', agent_name: 'Labs Data', started_at: '2026-10-20T10:00:00Z' }
  const member = (user_id: number, email: string, role: string, has_copy: boolean) =>
    ({ user_id, email, role, has_copy, sources: [], has_vault_key: true })
  const PASS = 'orchid lantern 42 vapor'

  beforeEach(() => {
    api.listMyAgentsWithVaults.mockResolvedValue({ items: [], total: 0 })
    api.listMyWorkspacesWithVaults.mockResolvedValue([])
    api.putKeyset.mockImplementation(async (b) => keysetOut({ exists: true, ...b }))
    chat.myKeyCopies.mockResolvedValue([])
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: PRF_OUT })
  })

  async function open(ks: ReturnType<typeof keysetOut> = lockedOut()) {
    api.getKeyset.mockResolvedValue(ks)
    render(<VaultKeyPanel />)
    fireEvent.click(await screen.findByTestId('new-key-open'))
    return screen.findByTestId('new-key')
  }
  const createButton = () => screen.getByTestId('new-key-create') as HTMLButtonElement
  const whenReady = () => waitFor(() => expect(createButton().disabled).toBe(false))
  function typePassphrase() {
    fireEvent.change(screen.getByLabelText('New vault passphrase'), { target: { value: PASS } })
    fireEvent.change(screen.getByLabelText('Repeat the passphrase'), { target: { value: PASS } })
  }

  it('says up front what it does not need and does not delete', async () => {
    await open()
    expect(screen.getByTestId('new-key-lead').textContent)
      .toContain('does not need your current passphrase, Secret Key or passkey, and nothing stored in any vault is deleted')
  })

  it('with no private conversations the screen is the one confirm: no question and no browser dialog', async () => {
    const confirm = vi.fn(() => true)
    vi.stubGlobal('confirm', confirm)
    await open()
    await whenReady()
    expect(screen.queryByTestId('new-key-conversations')).toBeNull()
    expect(api.putKeyset).not.toHaveBeenCalled()
    fireEvent.click(createButton())
    await waitFor(() => expect(api.putKeyset).toHaveBeenCalled())
    expect(api.putKeyset.mock.calls[0][0].replace).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByTestId('new-key')).toBeNull())
  })

  it('asks about private conversations with nothing preselected, and waits for an answer', async () => {
    chat.myKeyCopies.mockResolvedValue([copy])
    await open()
    expect((await screen.findByTestId('new-key-conversations')).textContent).toContain('1 private conversation')
    expect((screen.getByTestId('new-key-carry') as HTMLInputElement).checked).toBe(false)
    expect((screen.getByTestId('new-key-without') as HTMLInputElement).checked).toBe(false)
    await screen.findByTestId('new-key-no-vaults')
    expect(createButton().disabled).toBe(true)
    fireEvent.click(screen.getByTestId('new-key-without'))
    expect(createButton().disabled).toBe(false)
  })

  it('without carrying: never asks to unlock the old key, and names what was left behind', async () => {
    chat.myKeyCopies.mockResolvedValue([copy])
    await open()
    fireEvent.click(await screen.findByTestId('new-key-without'))
    await whenReady()
    fireEvent.click(createButton())
    await waitFor(() => expect(api.putKeyset).toHaveBeenCalled())
    expect(api.putKeyset.mock.calls[0][0].replace).toBe(true)
    expect(screen.queryByTestId('vault-unlock')).toBeNull()
    expect(chat.putMyKeyCopies).not.toHaveBeenCalled()
    const notice = await screen.findByTestId('vault-key-notice')
    expect(notice.textContent).toContain('not carried over')
    expect(notice.textContent).toContain('with Labs Data')
  })

  it('carrying over: opens each copy with the old key and re-wraps it to the new one', async () => {
    const me = await person()
    const scalar = new Uint8Array(32).fill(5)
    const seal = await zk.sealUserKey(me.scalar, USER, 'cred-1', PRF_OUT)
    const wrap = await zk.wrapEcdhEs(scalar, me.publicJwk, wrapInfo('c-1', USER))
    chat.myKeyCopies.mockResolvedValue([{ ...copy, wrap }])
    chat.putMyKeyCopies.mockResolvedValue({ stored: 1 })
    await open(keysetOut({ exists: true, public_jwk: me.publicJwk, seals: [seal],
                           passkeys: [{ credential_id: 'cred-1', label: 'mac', prf_enabled: true, sealed: true }] }))
    fireEvent.click(await screen.findByTestId('new-key-carry'))
    await whenReady()
    fireEvent.click(createButton())
    await waitFor(() => expect(chat.putMyKeyCopies).toHaveBeenCalled())
    const body = api.putKeyset.mock.calls[0][0]
    expect(body.replace).toBe(true)
    const newScalar = await zk.openUserKey(body.seals[0], USER, PRF_OUT)
    const newPrivate = await zk.importPrivateScalar(newScalar, body.public_jwk)
    const stored = chat.putMyKeyCopies.mock.calls[0][0][0]
    expect(Array.from(await zk.unwrapEcdhEs(stored.wrap, newPrivate, body.public_jwk, wrapInfo('c-1', USER))))
      .toEqual(Array.from(new Uint8Array(32).fill(5)))
    expect((await screen.findByTestId('vault-key-notice')).textContent).toContain('carried over to the new key')
  })

  it('a passkey that gives no PRF: says nothing changed, then a new passphrase and Secret Key, never the old key', async () => {
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: null })
    await open()
    await whenReady()
    fireEvent.click(createButton())
    expect((await screen.findByTestId('new-key-error')).textContent).toContain('nothing was changed')
    expect(api.putKeyset).not.toHaveBeenCalled()
    expect(createButton().disabled).toBe(true)
    typePassphrase()
    await whenReady()
    fireEvent.click(createButton())
    const code = (await screen.findByTestId('vault-secret-key', {}, { timeout: 5000 })).textContent!
    expect(screen.queryByTestId('vault-unlock')).toBeNull()
    const body = api.putKeyset.mock.calls[0][0]
    expect(body.replace).toBe(true)
    expect(body.seals[0].alg).toBe('2skd')
    expect((await zk.openUserKey2skd(body.seals[0], USER, PASS, zk.parseSecretKeyCode(code))).length).toBe(32)
    expect(screen.getByTestId('vault-emergency-kit')).toBeTruthy()
  })

  it('passkeys known not to report PRF: asks for the passphrase from the start, with no passkey touch', async () => {
    await open(keysetOut({ exists: true, public_jwk: { kty: 'EC', crv: 'P-256', x: 'x', y: 'y' }, seals: [{ alg: '2skd' }],
                           passkeys: [{ credential_id: 'cred-1', label: 'app', prf_enabled: false, sealed: false }] }))
    expect(screen.getByTestId('new-key-passphrase')).toBeTruthy()
    typePassphrase()
    await whenReady()
    fireEvent.click(createButton())
    await screen.findByTestId('vault-secret-key', {}, { timeout: 5000 })
    expect(prf.assertWithPrf).not.toHaveBeenCalled()
  })

  it('carrying with a passkey that cannot open the old key: points at the other answer, not a new passphrase', async () => {
    chat.myKeyCopies.mockResolvedValue([copy])
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: null })
    const me = await person()
    const seal = await zk.sealUserKey(me.scalar, USER, 'cred-1', PRF_OUT)
    await open(keysetOut({ exists: true, public_jwk: me.publicJwk, seals: [seal],
                           passkeys: [{ credential_id: 'cred-1', label: 'mac', prf_enabled: true, sealed: true }] }))
    fireEvent.click(await screen.findByTestId('new-key-carry'))
    await whenReady()
    fireEvent.click(createButton())
    expect((await screen.findByTestId('new-key-error')).textContent).toContain('without carrying them over')
    expect(screen.queryByTestId('new-key-passphrase')).toBeNull()
    expect(api.putKeyset).not.toHaveBeenCalled()
  })

  it('still shows the Secret Key when the key was replaced but carrying failed afterwards', async () => {
    const me = await person()
    const sk = zk.randomBytes(16)
    window.localStorage.setItem(`canopy-vault-secret-key:${USER}`, zk.secretKeyCode(sk))
    const seal = await zk.sealUserKey2skd(me.scalar, USER, 'the old passphrase here', sk)
    const wrap = await zk.wrapEcdhEs(new Uint8Array(32).fill(5), me.publicJwk, wrapInfo('c-1', USER))
    chat.myKeyCopies.mockResolvedValue([{ ...copy, wrap }])
    chat.putMyKeyCopies.mockRejectedValue(new Error('boom'))
    await open(keysetOut({ exists: true, public_jwk: me.publicJwk, seals: [seal],
                           passkeys: [{ credential_id: 'cred-1', label: 'app', prf_enabled: false, sealed: false }] }))
    fireEvent.click(await screen.findByTestId('new-key-carry'))
    typePassphrase()
    await whenReady()
    fireEvent.click(createButton())
    fireEvent.change(await screen.findByLabelText('Vault passphrase'), { target: { value: 'the old passphrase here' } })
    fireEvent.click(screen.getByTestId('vault-unlock-passphrase'))
    // The store retries with real backoff (3.5 s) around two passphrase derivations.
    await screen.findByTestId('vault-secret-key', {}, { timeout: 25000 })
    expect((await screen.findByTestId('vault-key-notice')).textContent).toContain('could not be carried over')
  }, 30000)

  it('lists each vault with who can let you back in, flags the ones only you can open, and skips the rest', async () => {
    api.listMyAgentsWithVaults.mockResolvedValue({ items: [
      { slug: 'eva', name: 'Eva' }, { slug: 'solo', name: 'Solo' }, { slug: 'other', name: 'Other' },
      { slug: 'homeless', name: 'Homeless' }, { slug: 'broken', name: 'Broken' }], total: 5 })
    api.getVaultMembers.mockImplementation(async (slug: string) => {
      if (slug === 'homeless') throw new api.VaultApiError(404, 'no vault')
      if (slug === 'broken') throw new api.VaultApiError(500, 'boom')
      const members = {
        eva: [member(7, 'me@x', 'member', true), member(8, 'owner@x', 'owner', true), member(9, 'waiting@x', 'admin', false)],
        // A plain member holding a copy cannot grant (granting is admin-gated), so this is still flagged.
        solo: [member(7, 'me@x', 'owner', true), member(8, 'plain@x', 'member', true)],
        other: [member(7, 'me@x', 'admin', false), member(8, 'owner@x', 'owner', true)],
      }[slug]
      return { vault_id: `v-${slug}`, members, holders: { keepers: [] } }
    })
    api.listMyWorkspacesWithVaults.mockResolvedValue([{ slug: 'connect', name: 'Connect' }, { slug: 'gone', name: 'Gone' }])
    api.listWorkspaceVaults.mockImplementation(async (slug: string) => {
      if (slug === 'gone') throw new Error('nope')
      return [{ vault_id: 'v-ws', name: 'Shared', members: [member(7, 'me@x', 'member', true)], holders: { keepers: ['box'] } }]
    })
    await open()
    const eva = await screen.findByTestId('new-key-vault-v-eva')
    expect(eva.textContent).toContain('owner@x can let you back in')
    expect(eva.textContent).not.toContain('waiting@x')
    expect(within(eva).queryByTestId('new-key-alone')).toBeNull()
    expect(within(screen.getByTestId('new-key-vault-v-solo')).getByTestId('new-key-alone').textContent)
      .toContain('Check with an owner or admin first')
    expect(screen.getByTestId('new-key-vault-v-ws').textContent).toContain('box (a machine) can let you back in')
    expect(screen.queryByTestId('new-key-vault-v-other')).toBeNull()
    // The vault nobody else can reopen comes first.
    expect(screen.getByTestId('new-key-vaults').firstElementChild).toBe(screen.getByTestId('new-key-vault-v-solo'))
    const unread = screen.getByTestId('new-key-unread').textContent!
    expect(unread).toContain('Broken')
    expect(unread).toContain('Gone')
    expect(unread).not.toContain('Homeless')
  })

  it('a failed listing reads as "could not read", never as "no vaults"', async () => {
    api.listMyAgentsWithVaults.mockRejectedValue(new Error('down'))
    await open()
    expect((await screen.findByTestId('new-key-unread')).textContent).toContain('your agents')
  })

  it('tells someone with the wrong passphrase there is a way out', async () => {
    const me = await person()
    const sk = zk.randomBytes(16)
    window.localStorage.setItem(`canopy-vault-secret-key:${USER}`, zk.secretKeyCode(sk))
    const seal = await zk.sealUserKey2skd(me.scalar, USER, 'the right passphrase', sk)
    await expect(unlockVaultKey(async () => ({ method: 'passphrase', passphrase: 'not the right one!' }),
      keysetOut({ exists: true, public_jwk: me.publicJwk, seals: [seal] }) as never))
      .rejects.toThrow('Lost your passphrase or Secret Key? Create a new vault key - it does not need them.')
    expect(api.reportVaultClient).toHaveBeenCalledWith(expect.objectContaining({
      stage: 'unlock', method: 'passphrase', outcome: 'failed', asked: ['passphrase'], secret_key_remembered: true,
      error: 'no passphrase seal opened (1 tried, Secret Key remembered)',
    }))
    expect(JSON.stringify(api.reportVaultClient.mock.calls)).not.toContain('not the right one!')
  }, 20000)

  it('reports a Secret Key that will not parse without any character of what was typed', async () => {
    const me = await person()
    const seal = await zk.sealUserKey2skd(me.scalar, USER, 'the right passphrase', zk.randomBytes(16))
    const typed = 'CV1-' + '#'.repeat(26)
    await expect(unlockVaultKey(async () => ({ method: 'passphrase', passphrase: 'the right passphrase', secretKey: typed }),
      keysetOut({ exists: true, public_jwk: me.publicJwk, seals: [seal] }) as never)).rejects.toThrow('Secret Key')
    const sent = JSON.stringify(api.reportVaultClient.mock.calls)
    expect(sent).toContain('Secret Key did not parse')
    expect(sent).not.toContain('#')
  })
})

describe('An agent page: nothing to set up by hand', () => {
  it('shows a born agent (no key of its own yet) as private, with no setup button', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    api.getZkState.mockResolvedValue({ agent: 'eva', agent_id: AGENT_ID, enabled: true, kv: 0, wraps: [],
      pending: [{ kind: 'device', target: 'dk1', runner_name: 'box' }], rotation_recommended: false, values: null,
      sealed: ['kindora'] })
    render(<AgentZkSection slug="eva" canEdit />)
    expect((await screen.findByTestId('zk-private')).textContent).toMatch(/Only you \(your passkey\) and the machines you approve can open these/)
    expect((await screen.findByTestId('zk-trust-caveat')).textContent).toBe(ZK_TRUST_CAVEAT)
    expect(screen.queryByRole('button')).toBeNull()
    expect(document.body.textContent).not.toMatch(/finish setup|\block\b/i)
  })

  it('an agent with its key: who can read it, and a button only when someone is waiting', async () => {
    api.getZkState.mockResolvedValue({ agent: 'eva', agent_id: AGENT_ID, enabled: true, kv: 1, wraps: [],
      pending: [], rotation_recommended: false, values: null, sealed: ['gog-token'] })
    render(<AgentZkSection slug="eva" canEdit />)
    expect((await screen.findByTestId('zk-on')).textContent).toMatch(/canopy-web can/)
    expect(screen.queryByTestId('zk-grant')).toBeNull()
    expect(screen.queryByTestId('zk-sealed')).toBeNull()
  })
})

describe('vaultKeyRoute', () => {
  it('passkey when one reported PRF; upgrade when none did yet; passphrase when all said no or the browser cannot', () => {
    expect(vaultKeyRoute([{ prf_enabled: true }], null)).toBe('passkey')
    expect(vaultKeyRoute([{ prf_enabled: null }], null)).toBe('upgrade')
    expect(vaultKeyRoute([], null)).toBe('upgrade')
    expect(vaultKeyRoute([{ prf_enabled: false }], null)).toBe('passphrase')
    expect(vaultKeyRoute([{ prf_enabled: true }], false)).toBe('passphrase')
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
    render(<AgentZkSection slug="eva" canEdit />)
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
  it('is not part of the default view — even with a key', async () => {
    api.getKeyset.mockResolvedValue(keysetOut({ exists: true, public_jwk: { kty: 'EC' }, seals: [{ alg: 'prf' }] }))
    window.location.hash = ''
    const { container } = render(<VaultKeyPanel onlyWhenLinked />)
    await waitFor(() => expect(api.getKeyset).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="settings-vault-key"]')).toBeNull()
  })

  it('shows when linked as #vault-key', async () => {
    api.getKeyset.mockResolvedValue(keysetOut())
    window.location.hash = '#vault-key'
    render(<VaultKeyPanel onlyWhenLinked />)
    expect(await screen.findByTestId('settings-vault-key')).toBeTruthy()
    window.location.hash = ''
  })
})
