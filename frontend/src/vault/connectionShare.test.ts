// The owner's browser opening, signing and re-sealing a connection, against the NORMATIVE
// vectors (docs/vault/zk-v1/connections_v1_vectors.json) and against what a compromised
// canopy-web could hand it. `connections_v1_browser_share.json` is what this code writes,
// checked in so tests/test_connections_browser_share.py can put it through the real route
// and the reference. Regenerate it with:
//   cd frontend && VITE_WRITE_SHARE_FIXTURE=1 npx vitest run src/vault/connectionShare.test.ts
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { ShareIn, ShareStateOut } from '@/api/connections'
import raw from '../../../docs/vault/zk-v1/connections_v1_vectors.json?raw'
import shareFixture from '../../../docs/vault/zk-v1/connections_v1_browser_share.json?raw'
import {
  buildShare,
  changedEndorser,
  chooseRecipients,
  entryDigest,
  envelopeMac,
  envelopeMacInfo,
  envelopeMsg,
  leftOut,
  openForOwner,
  signStatement,
  statementMessage,
  trustFor,
  verifyStatement,
  writerSig,
  ShareRefused,
  type Statement,
  type Trust,
} from './connectionShare'
import type { Pins } from './trust'
import type { Unlocked } from './userKey'
import {
  b64u,
  endorse,
  hexToBytes,
  importPrivateScalar,
  jkt,
  openSealedValue,
  publicJwkOfScalar,
  sealValue,
  unb64u,
  valueDigest,
  type Bytes,
  type PublicJwk,
} from './zk'

const v = JSON.parse(raw)
const dec = new TextDecoder()

const CONNECTION = v.statement.statement.connection as string
const BINDING = `connection:${CONNECTION}`
const ACCOUNT = v.statement.statement.account_id as string
const OWNER = '70007'
const ADA = '70008'
const STRANGER = '70009'
const EVA = '70012'
const ECHO = '70100'
const REFRESH_TOKEN = '5Aep861-browser-refresh-token'
const ACCESS_TOKEN = '00D-browser-access-token'

interface Who { scalar: Bytes; pub: PublicJwk; jkt: string; priv: CryptoKey }

async function who(hex: string): Promise<Who> {
  const scalar = hexToBytes(hex.repeat(32))
  const pub = await publicJwkOfScalar(scalar)
  return { scalar, pub, jkt: await jkt(pub), priv: await importPrivateScalar(scalar, pub) }
}

let owner: Who, ada: Who, stranger: Who, evaBox: Who, adaBox: Who, echoBox: Who, strangerBox: Who, computer: Who, me: Unlocked

beforeAll(async () => {
  ;[owner, ada, stranger, evaBox, adaBox, echoBox, strangerBox, computer] = await Promise.all(
    ['7a', 'ad', 'be', '8b', '9c', 'd1', 'e2', 'f3'].map(who))
  me = { userId: OWNER, privateKey: owner.priv, publicJwk: owner.pub, scalar: owner.scalar }
})

function refreshPlain(over: Record<string, unknown> = {}) {
  return { v: 1, provider: 'salesforce', refresh_token: REFRESH_TOKEN, client_id: '3MVG9-test-client-id',
           login_host: 'login.salesforce.com', instance_url: 'https://acme.my.salesforce.com', account_id: ACCOUNT,
           owner_key: owner.jkt, issued_at: 1793500000, scopes: 'api refresh_token id', statement_floor: 0, ...over }
}

function accessPlain(over: Record<string, unknown> = {}) {
  return { v: 1, access_token: ACCESS_TOKEN, expires_at: 1793501800, instance_url: 'https://acme.my.salesforce.com',
           account_id: ACCOUNT, scope: 'api', owner_key: owner.jkt, statement_floor: 0, ...over }
}

const ownerRecipient = () => ({ kind: 'member' as const, target: OWNER, publicJwk: owner.pub })
const toDevice = (d: Who) => ({ kind: 'device' as const, target: d.jkt, publicJwk: d.pub })

function device(d: Who, agentId: string, runner: string, mine = false) {
  return { kind: 'device' as const, target: d.jkt, public_jwk: { ...d.pub }, agent_id: agentId, runner_name: runner, mine }
}

async function vouch(by: Who, byId: string, agentId: string, d: Who) {
  const e = await endorse(by.scalar, by.pub, agentId, 'device', d.jkt, d.pub)
  return { ...e, by: byId, by_email: `${byId}@x`, by_public_jwk: { ...by.pub }, agent_id: agentId }
}

/** What canopy-web answers right after connect, with every kind of machine a share meets. */
async function unshared(over: Record<string, unknown> = {}, plain: Record<string, unknown> = {}): Promise<ShareStateOut> {
  const sealed = await sealValue(JSON.stringify(refreshPlain(plain)), BINDING, 'refresh', 0, [ownerRecipient()])
  return {
    id: CONNECTION, binding: BINDING, provider: 'salesforce', account_id: ACCOUNT, account_label: 'jj@acme.test',
    state: 'unshared', sealed, sealed_sig: null, sealed_digest: await valueDigest(sealed),
    access_sealed: '', access_sig: null, statement: null, statement_sig: '', endorsers: [], owner_agent: null,
    owner: { user_id: OWNER, public_jwk: { ...owner.pub } },
    candidates: [
      { kind: 'member', target: OWNER, public_jwk: { ...owner.pub }, agent_id: '', runner_name: '', mine: true },
      device(evaBox, EVA, 'eva-cloud'),
      device(adaBox, EVA, 'ada-cloud'),
      device(strangerBox, EVA, 'stranger-cloud'),
      device(echoBox, ECHO, 'echo-cloud'),
      device(computer, '', 'jj-computer', true),
    ],
    endorsements: [
      await vouch(owner, OWNER, EVA, evaBox),
      await vouch(ada, ADA, EVA, adaBox),
      await vouch(stranger, STRANGER, EVA, strangerBox),
      await vouch(owner, OWNER, ECHO, echoBox),
    ],
    agents: [{ id: EVA, slug: 'eva', name: 'Eva' }, { id: ECHO, slug: 'echo', name: 'Echo' }],
    ...over,
  } as unknown as ShareStateOut
}

const PINS = (): Pins => ({ [ADA]: { jkt: ada.jkt, email: 'ada@x', pinned_at: '2026-01-01T00:00:00Z' } })

/** This browser's pins, as `buildShare` reads them (`vault/trust.ts` `loadPins`). */
function pinAda() {
  vi.stubGlobal('window', { localStorage: { getItem: () => JSON.stringify(PINS()), setItem: () => {}, removeItem: () => {} } })
}

afterEach(() => vi.unstubAllGlobals())

const statementOf = (over: Partial<Statement> = {}): Statement => ({
  account_id: ACCOUNT, agents: [EVA], connection: CONNECTION, endorsers: [], my_computers: [], v: 1, version: 3, ...over })

/** The same state once a machine has written back: signed by that machine, with an access token. */
async function written(state: ShareStateOut, writer: Who, opts: {
  statement?: Statement; signedBy?: Who; refresh?: Record<string, unknown>; access?: Record<string, unknown> | null; to?: Who[]
} = {}): Promise<ShareStateOut> {
  const statement = opts.statement ?? statementOf({ my_computers: [] })
  const signer = opts.signedBy ?? owner
  const statement_sig = await signStatement(statement, signer.scalar, signer.pub)
  const recipients = [ownerRecipient(), ...(opts.to ?? [writer]).map(toDevice)]
  const by = { kind: 'device' as const, target: writer.jkt, privateKey: writer.priv, publicJwk: writer.pub }
  const sealed = await sealValue(JSON.stringify(refreshPlain({ statement_floor: statement.version, ...opts.refresh })),
    BINDING, 'refresh', 0, recipients)
  const prior = state.sealed_digest
  const sealed_sig = await writerSig(BINDING, 'refresh', prior, sealed, by, recipients)
  const out: Record<string, unknown> = { ...state, state: 'active', statement, statement_sig, sealed, sealed_sig,
                                         sealed_digest: await valueDigest(sealed), endorsers: statement.endorsers }
  if (opts.access !== null) {
    const access_sealed = await sealValue(JSON.stringify(accessPlain({ statement_floor: statement.version, ...opts.access })),
      BINDING, 'access', 0, recipients)
    out.access_sealed = access_sealed
    out.access_sig = await writerSig(BINDING, 'access', '', access_sealed, by, recipients)
  }
  return out as unknown as ShareStateOut
}

function refused(p: Promise<unknown>, code: string) {
  return expect(p).rejects.toSatisfy((e: unknown) => e instanceof ShareRefused && e.code === code,
    `refused with ${code}`)
}

/** What a recipient's check of its MAC is: its own key against the writer's. */
async function macOk(stored: string, sig: Record<string, unknown>, name: string, kind: string, target: string, priv: CryptoKey) {
  const entries = JSON.parse(dec.decode(unb64u(stored.slice(5)))).r as { kind: string; target: string }[]
  const entry = entries.find((e) => e.kind === kind && e.target === target)
  const macs = sig.macs as { kind: string; target: string; mac: string }[]
  const mine = macs.find((m) => m.kind === kind && m.target === target)
  if (!entry || !mine) return false
  const by = sig.by as { public_jwk: PublicJwk }
  return (await envelopeMac(BINDING, name, sig.expect as string, entry as never, priv, by.public_jwk)) === mine.mac
}

const sealedTargets = (stored: string) =>
  (JSON.parse(dec.decode(unb64u(stored.slice(5)))).r as { kind: string; target: string }[])
    .map((e) => `${e.kind}:${e.target}`).sort()

describe('the grant statement', () => {
  it('statementMessage matches the vector byte for byte', () => {
    expect(statementMessage(v.statement.statement)).toBe(v.statement.msg)
  })

  it('writes the same bytes whatever order the keys arrive in', () => {
    const s = v.statement.statement
    const shuffled = { version: s.version, v: s.v, my_computers: s.my_computers, endorsers: s.endorsers,
                       connection: s.connection, agents: s.agents, account_id: s.account_id }
    expect(statementMessage(shuffled)).toBe(v.statement.msg)
  })

  it.each([
    ['a leading zero', { agents: ['012'] }],
    ['agents out of string order', { agents: ['40', '12'] }],
    ['a repeated agent', { agents: ['12', '12'] }],
    ['a numeric agent', { agents: [12] }],
    ['a short thumbprint', { my_computers: ['abc'] }],
    ['an unsorted endorsers list', { endorsers: ['Z'.repeat(43), 'A'.repeat(43)] }],
    ['too many endorsers', { endorsers: Array.from({ length: 65 }, (_, i) => String(i).padStart(43, '0')) }],
    ['non-ASCII in the account', { account_id: 'café' }],
    ['a quote in the account', { account_id: 'a"b' }],
    ['the wrong v', { v: 2 }],
    ['version 0', { version: 0 }],
    ['an upper-case connection', { connection: CONNECTION.toUpperCase() }],
    ['a key too many', { extra: 1 }],
  ])('refuses %s', (_why, over) => {
    expect(() => statementMessage({ ...v.statement.statement, ...over })).toThrow(ShareRefused)
  })

  it('accepts the reference signature', async () => {
    expect(await verifyStatement(v.statement.statement, v.statement.sig, v.statement.signer_jwk)).toBe(true)
    expect(await verifyStatement({ ...v.statement.statement, version: 4 }, v.statement.sig, v.statement.signer_jwk)).toBe(false)
  })

  it('a statement signed here verifies with the reference key', async () => {
    const sig = await signStatement(v.statement.statement, hexToBytes(v.statement.signer_priv_scalar_hex), v.statement.signer_jwk)
    expect(unb64u(sig)).toHaveLength(64)
    expect(await verifyStatement(v.statement.statement, sig, v.statement.signer_jwk)).toBe(true)
  })
})

describe('writer MACs against the vectors', () => {
  const e = () => v.envelope
  const privOf = (hex: string, pub: PublicJwk) => importPrivateScalar(hexToBytes(hex), pub)
  const entries = () => JSON.parse(dec.decode(unb64u(e().sealed.slice(5)))).r
  const expectOf = (w: string) => e()[w].sig.expect as string

  it('reproduces each entry digest, message and MAC info', async () => {
    for (const [i, entry] of entries().entries()) {
      const want = e().entries[i]
      expect(await entryDigest(entry)).toBe(want.entry_digest)
      expect(await envelopeMsg(e().binding, 'refresh', expectOf('device_writer'), entry)).toBe(want.device_writer_msg)
      expect(envelopeMacInfo(e().binding, 'refresh', entry.kind, entry.target)).toBe(want.mac_info)
    }
  })

  it.each([
    ['device_writer', 'device', 'wF73aO2oJC_yXdBkgGCfRX9F-pVYeLX8vrexE117UO8'],
    ['member_writer', 'member', '7'],
  ])('reproduces the %s sig byte for byte', async (w, kind, target) => {
    const want = e()[w].sig
    const privateKey = await privOf(e()[w].priv_scalar_hex, want.by.public_jwk)
    const recipients = e().recipients.map((r: { kind: 'device' | 'member'; target: string; public_jwk: PublicJwk }) =>
      ({ kind: r.kind, target: r.target, publicJwk: r.public_jwk }))
    const sig = await writerSig(e().binding, 'refresh', want.expect, e().sealed,
      { kind: kind as 'device' | 'member', target, privateKey, publicJwk: want.by.public_jwk }, recipients)
    expect(JSON.stringify(sig)).toBe(JSON.stringify(want))
  })

  it('a recipient re-derives the writer MAC with its own key', async () => {
    const want = e().member_writer.sig
    for (const r of e().recipients) {
      const priv = await privOf(r.priv_scalar_hex, r.public_jwk)
      const entry = entries().find((x: { kind: string; target: string }) => x.kind === r.kind && x.target === r.target)
      const mac = want.macs.find((m: { kind: string; target: string }) => m.kind === r.kind && m.target === r.target).mac
      expect(await envelopeMac(e().binding, 'refresh', want.expect, entry, priv, want.by.public_jwk)).toBe(mac)
    }
  })
})

describe('openForOwner', () => {
  const vectorOwner = async (): Promise<Unlocked> => {
    const r = v.envelope.recipients[2]
    return { userId: '7', privateKey: await importPrivateScalar(hexToBytes(r.priv_scalar_hex), r.public_jwk),
             publicJwk: r.public_jwk, scalar: hexToBytes(r.priv_scalar_hex) }
  }
  const vectorState = (over: Record<string, unknown>) => ({
    id: CONNECTION, binding: BINDING, provider: 'salesforce', account_id: ACCOUNT, account_label: 'x', state: 'active',
    sealed: v.envelope.sealed, sealed_sig: v.envelope.member_writer.sig, sealed_digest: v.envelope.sealed_digest,
    access_sealed: '', access_sig: null, statement: v.statement.statement, endorsers: v.statement.statement.endorsers,
    owner: { user_id: '7', public_jwk: v.statement.signer_jwk }, candidates: [], endorsements: [], agents: [], ...over,
  }) as unknown as ShareStateOut

  it('opens the reference envelope written by the owner', async () => {
    const { account } = await openForOwner(vectorState({}), await vectorOwner())
    expect(account).toEqual({ account_id: ACCOUNT, instance_url: 'https://acme.my.salesforce.com', provider: 'salesforce' })
  })

  it('opens the reference envelope written by a machine the statement allows', async () => {
    const writer = v.envelope.device_writer.sig.by.target as string
    const statement = { ...v.statement.statement, my_computers: [v.statement.statement.my_computers[0], writer].sort() }
    const statement_sig = await signStatement(statement, hexToBytes(v.statement.signer_priv_scalar_hex), v.statement.signer_jwk)
    await expect(openForOwner(vectorState({ sealed_sig: v.envelope.device_writer.sig, statement, statement_sig }), await vectorOwner()))
      .resolves.toBeTruthy()
  })

  it('refuses the reference envelope written by a machine the statement does not', async () => {
    await refused(openForOwner(vectorState({ sealed_sig: v.envelope.device_writer.sig }), await vectorOwner()), 'bad-writer')
  })

  it('accepts the unsigned first seal only while unshared', async () => {
    const opened = await openForOwner(await unshared(), me)
    expect(opened.account.account_id).toBe(ACCOUNT)
    await expect(openForOwner(await unshared({ sealed_sig: undefined }), me)).resolves.toBeTruthy()
  })

  it('refuses an unsigned envelope once shared', async () => {
    await refused(openForOwner(await unshared({ state: 'active', statement: statementOf({ version: 1 }) }), me), 'unsigned')
    await refused(openForOwner(await unshared({ state: 'revoking' }), me), 'unsigned')
  })

  it('never returns the refresh token to callers', async () => {
    const opened = await openForOwner(await written(await unshared(), evaBox, {
      statement: statementOf({ my_computers: [evaBox.jkt] }) }), me)
    expect(Object.keys(opened)).toEqual(['account'])
    expect(Object.keys(opened.account)).toEqual(['account_id', 'instance_url', 'provider'])
    expect(JSON.stringify(opened)).not.toContain(REFRESH_TOKEN)
    expect(JSON.stringify(opened)).not.toContain(ACCESS_TOKEN)
  })

  it('a refusal names no secret', async () => {
    const e = await openForOwner(await unshared({}, { owner_key: 'x'.repeat(43) }), me).catch((x: Error) => x)
    expect(e).toBeInstanceOf(ShareRefused)
    expect(String((e as Error).message)).not.toContain(REFRESH_TOKEN)
  })

  it('still checks a MAC when an unshared envelope carries one', async () => {
    const s = await written(await unshared(), evaBox, { statement: statementOf({ my_computers: [evaBox.jkt] }), access: null })
    const sig = s.sealed_sig as { macs: { kind: string; target: string; mac: string }[] }
    const bad = { ...sig, macs: sig.macs.map((m) => ({ ...m, mac: b64u(new Uint8Array(32)) })) }
    await refused(openForOwner({ ...s, state: 'unshared', sealed_sig: bad } as ShareStateOut, me), 'bad-mac')
  })

  it('refuses a MAC list that does not cover the recipients', async () => {
    const s = await written(await unshared(), evaBox, { statement: statementOf({ my_computers: [evaBox.jkt] }), access: null })
    const sig = s.sealed_sig as { macs: { kind: string; target: string; mac: string }[] }
    await refused(openForOwner({ ...s, sealed_sig: { ...sig, macs: sig.macs.slice(0, 1) } } as ShareStateOut, me), 'bad-sig')
  })

  it('refuses an envelope written by a machine the statement does not allow', async () => {
    await refused(openForOwner(await written(await unshared(), strangerBox, { access: null }), me), 'bad-writer')
  })

  it('takes an endorsement as the statement says, not as the browser pins', async () => {
    // ada is pinned here, but the statement does not name her: her machine may not write.
    const base = await unshared()
    await refused(openForOwner(await written(base, adaBox, { access: null }), me), 'bad-writer')
    await expect(openForOwner(await written(base, adaBox, {
      access: null, statement: statementOf({ endorsers: [ada.jkt] }) }), me)).resolves.toBeTruthy()
  })

  describe('a device writer rests on a statement the owner signed', () => {
    // canopy-web, compromised: its own box, a substitute token, the real owner_key, and a
    // statement naming that box. Everything agrees except who signed the statement.
    const forged = async (over: Parameters<typeof written>[2]) => written(await unshared(), strangerBox, {
      statement: statementOf({ my_computers: [strangerBox.jkt] }), refresh: { refresh_token: 'substitute' }, access: null, ...over })

    it('refuses a fabricated statement signed by anyone else', async () => {
      await refused(openForOwner(await forged({ signedBy: stranger }), me), 'bad-writer')
      await refused(buildShare(await forged({ signedBy: stranger }), me, { agents: [], myComputers: [] }), 'bad-writer')
    })

    it('refuses the owner’s real signature over a different statement', async () => {
      const real = await signStatement(statementOf(), owner.scalar, owner.pub)
      const s = await forged({ signedBy: owner })
      await refused(openForOwner({ ...s, statement_sig: real } as ShareStateOut, me), 'bad-writer')
    })

    it.each([[''], [undefined], [null], ['not base64!']])('refuses a statement signature of %j', async (sig) => {
      const s = await forged({ signedBy: owner })
      await refused(openForOwner({ ...s, statement_sig: sig } as unknown as ShareStateOut, me), 'bad-writer')
    })

    it('refuses a fabricated statement that would admit an endorsed writer too', async () => {
      const s = await written(await unshared(), adaBox, { access: null, signedBy: stranger,
                                                          statement: statementOf({ endorsers: [ada.jkt] }) })
      await refused(openForOwner(s, me), 'bad-writer')
    })

    it('checks the same signature for the writer of access', async () => {
      const real = await written(await unshared(), evaBox, {})
      const s = await written(await unshared(), strangerBox, { statement: statementOf({ my_computers: [strangerBox.jkt] }) })
      // `refresh` by the owner's own key, `access` by a machine a fabricated statement admits.
      const sealed_sig = await writerSig(BINDING, 'refresh', '', real.sealed,
        { kind: 'member', target: OWNER, privateKey: owner.priv, publicJwk: owner.pub }, [ownerRecipient(), toDevice(evaBox)])
      await refused(openForOwner({ ...real, sealed_sig, statement: s.statement, statement_sig: await signStatement(statementOf(), stranger.scalar, stranger.pub),
                                   access_sealed: s.access_sealed, access_sig: s.access_sig } as ShareStateOut, me), 'bad-writer')
    })

    it('needs no signature when the writer is the owner', async () => {
      const state = await unshared({ state: 'unshared', statement: statementOf({ version: 1 }), statement_sig: '' })
      await expect(openForOwner(state, me)).resolves.toBeTruthy()
    })
  })

  it('refuses an endorsement for an agent the statement does not list', async () => {
    const base = await unshared()
    await refused(openForOwner(await written(base, echoBox, { access: null, statement: statementOf({ agents: [EVA] }) }), me), 'bad-writer')
  })

  it.each([
    ['owner_key', { owner_key: 'x'.repeat(43) }, 'bad-owner-key'],
    ['account', { account_id: 'someone/else' }, 'account-mismatch'],
    ['provider', { provider: 'google' }, 'bad-plaintext'],
    ['field set', { surprise: 1 }, 'bad-plaintext'],
    ['floor type', { statement_floor: '0' }, 'bad-plaintext'],
    ['version', { v: 2 }, 'bad-plaintext'],
    ['login host', { login_host: 'evil.example' }, 'bad-host'],
    ['instance', { instance_url: 'https://evil.example' }, 'bad-host'],
    ['instance scheme', { instance_url: 'http://acme.my.salesforce.com' }, 'bad-host'],
    ['floor above any statement', { statement_floor: 1 }, 'statement-below-floor'],
  ])('refuses a first seal with the wrong %s', async (_what, plain, code) => {
    await refused(openForOwner(await unshared({}, plain), me), code)
  })

  it('refuses when canopy-web names another key or another person as the owner', async () => {
    await refused(openForOwner(await unshared({ owner: { user_id: OWNER, public_jwk: { ...evaBox.pub } } }), me), 'bad-owner-key')
    await refused(openForOwner(await unshared({ owner: { user_id: ADA, public_jwk: { ...owner.pub } } }), me), 'bad-owner-key')
  })

  it('refuses a binding that is not this connection', async () => {
    await refused(openForOwner(await unshared({ binding: 'connection:00000000-0000-4000-8000-000000000000' }), me), 'bad-binding')
  })

  it('refuses a stored statement about another connection or account', async () => {
    await refused(openForOwner(await unshared({ statement: statementOf({ account_id: 'other/one', version: 1 }) }), me), 'bad-statement')
    await refused(openForOwner(await unshared({
      statement: statementOf({ connection: '00000000-0000-4000-8000-000000000000', version: 1 }) }), me), 'bad-statement')
  })

  it('refuses a statement below the refresh floor', async () => {
    const s = await written(await unshared(), evaBox, {
      statement: statementOf({ my_computers: [evaBox.jkt], version: 3 }), refresh: { statement_floor: 4 }, access: null })
    await refused(openForOwner(s, me), 'statement-below-floor')
  })

  it('refuses a statement below the access floor, even with a low refresh floor', async () => {
    const s = await written(await unshared(), evaBox, {
      statement: statementOf({ my_computers: [evaBox.jkt], version: 3 }), refresh: { statement_floor: 0 }, access: { statement_floor: 4 } })
    await refused(openForOwner(s, me), 'statement-below-floor')
  })

  it('reads an access left over from before a share (a lower floor than refresh)', async () => {
    const s = await written(await unshared(), evaBox, {
      statement: statementOf({ my_computers: [evaBox.jkt], version: 3 }), access: { statement_floor: 2 } })
    await expect(openForOwner(s, me)).resolves.toBeTruthy()
  })

  it.each([
    ['owner_key', { owner_key: 'y'.repeat(43) }, 'access-mismatch'],
    ['account', { account_id: 'someone/else' }, 'access-mismatch'],
    ['field set', { surprise: 1 }, 'bad-plaintext'],
    ['instance', { instance_url: 'https://evil.example' }, 'bad-host'],
  ])('refuses an access with the wrong %s', async (_what, access, code) => {
    const s = await written(await unshared(), evaBox, { statement: statementOf({ my_computers: [evaBox.jkt] }), access })
    await refused(openForOwner(s, me), code)
  })

  it('refuses an unsigned access the owner can open', async () => {
    const s = await written(await unshared(), evaBox, { statement: statementOf({ my_computers: [evaBox.jkt] }) })
    await refused(openForOwner({ ...s, access_sig: null } as ShareStateOut, me), 'unsigned')
  })

  it('refuses a forged MAC on an access', async () => {
    const s = await written(await unshared(), evaBox, { statement: statementOf({ my_computers: [evaBox.jkt] }) })
    const sig = s.access_sig as { macs: { kind: string; target: string; mac: string }[] }
    const bad = { ...sig, macs: sig.macs.map((m) => ({ ...m, mac: b64u(new Uint8Array(32)) })) }
    await refused(openForOwner({ ...s, access_sig: bad } as ShareStateOut, me), 'bad-mac')
  })
})

describe('chooseRecipients', () => {
  let state: ShareStateOut, trust: Trust

  beforeAll(async () => {
    state = await unshared()
    trust = await trustFor(state, me, { pins: PINS() })
  })

  const targets = (agents: string[], myComputers: string[] = []) =>
    chooseRecipients(state, { agents, myComputers }, trust).map((r) => `${r.kind}:${r.target}`).sort()

  it('always includes the owner', () => {
    expect(targets([])).toEqual([`member:${OWNER}`])
  })

  it('takes a device the owner endorsed, or a pinned person did', () => {
    expect(targets([EVA])).toEqual([`device:${evaBox.jkt}`, `device:${adaBox.jkt}`, `member:${OWNER}`].sort())
  })

  it('leaves out a device whose endorsement is by nobody this browser trusts', () => {
    expect(targets([EVA])).not.toContain(`device:${strangerBox.jkt}`)
    expect(leftOut(state, { agents: [EVA], myComputers: [] }, trust)).toEqual([
      { target: strangerBox.jkt, runner_name: 'stranger-cloud', agent_id: EVA, why: 'not vouched for by anyone you trust' },
    ])
  })

  it('takes nobody for an agent that was not chosen', () => {
    expect(targets([EVA])).not.toContain(`device:${echoBox.jkt}`)
    expect(leftOut(state, { agents: [ECHO], myComputers: [] }, trust)).toEqual([])
  })

  it('takes the owner’s own machine by choice, with no endorsement', () => {
    expect(targets([], [computer.jkt])).toEqual([`device:${computer.jkt}`, `member:${OWNER}`].sort())
  })

  it('does not take someone else’s machine as the owner’s own', async () => {
    const s = await unshared({ candidates: [device(strangerBox, '', 'not-mine', false)] })
    const t = await trustFor(s, me, { pins: PINS() })
    const choice = { agents: [], myComputers: [strangerBox.jkt] }
    expect(chooseRecipients(s, choice, t).map((r) => r.kind)).toEqual(['member'])
    expect(leftOut(s, choice, t)).toEqual([expect.objectContaining({ target: strangerBox.jkt, runner_name: 'not-mine' })])
  })

  it('names a chosen computer canopy-web does not list', async () => {
    const choice = { agents: [], myComputers: [strangerBox.jkt] }
    expect(leftOut(state, choice, trust)).toEqual([
      { target: strangerBox.jkt, runner_name: '', agent_id: '', why: 'not one of your enrolled computers' }])
  })

  it('leaves out a key that does not match its thumbprint', async () => {
    const s = await unshared({ candidates: [{ ...device(computer, '', 'swapped', true), public_jwk: { ...strangerBox.pub } },
                                            { ...device(evaBox, EVA, 'swapped-box'), public_jwk: { ...strangerBox.pub } }] })
    const t = await trustFor(s, me, { pins: PINS() })
    const choice = { agents: [EVA], myComputers: [computer.jkt] }
    expect(chooseRecipients(s, choice, t).map((r) => r.kind)).toEqual(['member'])
    expect(leftOut(s, choice, t).map((l) => l.why)).toEqual(['its key does not match its thumbprint', 'its key does not match its thumbprint'])
  })

  it('leaves out a key that is not on the curve, and shares with the rest', async () => {
    const off = { kty: 'EC', crv: 'P-256', x: b64u(new Uint8Array(32).fill(1)), y: b64u(new Uint8Array(32).fill(2)) }
    const offJkt = await jkt(off)
    const s = await unshared({ candidates: [device(evaBox, EVA, 'eva-cloud'),
      { kind: 'device', target: offJkt, public_jwk: off, agent_id: '', runner_name: 'off-curve', mine: true }] })
    const t = await trustFor(s, me, { pins: PINS() })
    const choice = { agents: [EVA], myComputers: [offJkt] }
    expect(chooseRecipients(s, choice, t).map((r) => r.target)).toEqual([OWNER, evaBox.jkt])
    expect(leftOut(s, choice, t)[0].runner_name).toBe('off-curve')
  })

  it('does not count an endorsement signed for another agent', async () => {
    const s = await unshared({ endorsements: [await vouch(owner, OWNER, ECHO, evaBox)].map((e) => ({ ...e, agent_id: EVA })) })
    const t = await trustFor(s, me, { pins: PINS() })
    expect(chooseRecipients(s, { agents: [EVA], myComputers: [] }, t).map((r) => r.kind)).toEqual(['member'])
  })

  it('does not count an endorsement of another key', async () => {
    const e = await vouch(owner, OWNER, EVA, strangerBox)
    const s = await unshared({ endorsements: [{ ...e, target: evaBox.jkt }, { ...e, target: evaBox.jkt, key_jkt: evaBox.jkt }] })
    const t = await trustFor(s, me, { pins: PINS() })
    expect(chooseRecipients(s, { agents: [EVA], myComputers: [] }, t).map((r) => r.kind)).toEqual(['member'])
  })

  it('does not take canopy-web’s word that the signer is someone pinned', async () => {
    // The stranger's key claiming to be ada: her pin holds another key.
    const s = await unshared({ endorsements: [await vouch(stranger, ADA, EVA, strangerBox)] })
    const t = await trustFor(s, me, { pins: PINS() })
    const choice = { agents: [EVA], myComputers: [] }
    expect(chooseRecipients(s, choice, t).map((r) => r.kind)).toEqual(['member'])
    expect(changedEndorser(t, choice)).toMatchObject({ by: ADA, status: 'changed' })
  })

  it('does not take the owner’s id with another key', async () => {
    const s = await unshared({ endorsements: [await vouch(stranger, OWNER, EVA, strangerBox)] })
    const t = await trustFor(s, me, { pins: PINS() })
    expect(chooseRecipients(s, { agents: [EVA], myComputers: [] }, t).map((r) => r.kind)).toEqual(['member'])
  })
})

describe('buildShare', () => {
  const withPins = (state: ShareStateOut, choice = { agents: [EVA, ECHO], myComputers: [computer.jkt] }) => {
    pinAda()
    return buildShare(state, me, choice)
  }
  const open = async (stored: string, name: string, kind: string, target: string, priv: CryptoKey) =>
    JSON.parse(await openSealedValue(stored, BINDING, name, kind, target, priv))

  it('signs a next statement the reference accepts', async () => {
    const state = await unshared()
    const share = await withPins(state)
    const st = share.statement as unknown as Statement
    expect(Object.keys(st)).toEqual(['account_id', 'agents', 'connection', 'endorsers', 'my_computers', 'v', 'version'])
    expect(st).toEqual({ account_id: ACCOUNT, agents: [EVA, ECHO].sort(), connection: CONNECTION, endorsers: [ada.jkt],
                         my_computers: [computer.jkt], v: 1, version: 1 })
    expect(await verifyStatement(st, share.sig, owner.pub)).toBe(true)
    expect(share.by_jkt).toBe(owner.jkt)
    expect(share.expect).toBe(state.sealed_digest)
  })

  it('moves the version on from the stored statement', async () => {
    const state = await written(await unshared(), evaBox, { statement: statementOf({ version: 3 }), access: null })
    const share = await withPins(state, { agents: [EVA], myComputers: [] })
    expect((share.statement as unknown as Statement).version).toBe(4)
  })

  it('sorts agents as strings and removes repeats', async () => {
    const share = await withPins(await unshared(), { agents: ['100', '40', '12', '40'], myComputers: [] })
    expect((share.statement as unknown as Statement).agents).toEqual(['100', '12', '40'])
  })

  it.each([['012'], ['abc'], ['']])('refuses the chosen agent %j', async (bad) => {
    await refused(withPins(await unshared(), { agents: [bad], myComputers: [] }), 'bad-statement')
  })

  it('lists only trusted endorsers of the chosen machines, and never the owner', async () => {
    const onlyEcho = await withPins(await unshared(), { agents: [ECHO], myComputers: [] })
    expect((onlyEcho.statement as unknown as Statement).endorsers).toEqual([])
    const eva = await withPins(await unshared(), { agents: [EVA], myComputers: [] })
    expect((eva.statement as unknown as Statement).endorsers).toEqual([ada.jkt])
    expect((eva.statement as unknown as Statement).endorsers).not.toContain(stranger.jkt)
    expect((eva.statement as unknown as Statement).endorsers).not.toContain(owner.jkt)
  })

  it('seals to the owner and chosen devices with a MAC per recipient', async () => {
    const state = await unshared()
    const share = await withPins(state)
    const want = [`member:${OWNER}`, `device:${evaBox.jkt}`, `device:${adaBox.jkt}`, `device:${echoBox.jkt}`,
                  `device:${computer.jkt}`].sort()
    expect(sealedTargets(share.sealed)).toEqual(want)
    const sig = share.sealed_sig as Record<string, unknown>
    const macs = (sig.macs as { kind: string; target: string }[]).map((m) => `${m.kind}:${m.target}`).sort()
    expect(macs).toEqual(want)
    expect(sig.expect).toBe(state.sealed_digest)
    expect(sig.by).toEqual({ kind: 'member', target: OWNER, public_jwk: owner.pub })
    for (const [kind, target, priv] of [['member', OWNER, owner.priv], ['device', evaBox.jkt, evaBox.priv],
                                        ['device', adaBox.jkt, adaBox.priv], ['device', echoBox.jkt, echoBox.priv],
                                        ['device', computer.jkt, computer.priv]] as const) {
      expect(await macOk(share.sealed, sig, 'refresh', kind, target, priv)).toBe(true)
      const plain = await open(share.sealed, 'refresh', kind, target, priv)
      expect(plain).toEqual(refreshPlain({ statement_floor: 1 }))
    }
    await expect(open(share.sealed, 'refresh', 'device', strangerBox.jkt, strangerBox.priv)).rejects.toThrow()
  })

  it('names the writer by exactly a public key, whatever WebCrypto hung on it', async () => {
    const noisy: Unlocked = { ...me, publicJwk: { ...owner.pub, ext: true, key_ops: ['deriveBits'] } as never }
    const share = await buildShare(await unshared(), noisy, { agents: [], myComputers: [] })
    const by = (share.sealed_sig as { by: { public_jwk: object } }).by.public_jwk
    expect(Object.keys(by)).toEqual(['kty', 'crv', 'x', 'y'])
  })

  it('writes the key it holds, not the one canopy-web named for it', async () => {
    const lied: Unlocked = { ...me, publicJwk: { ...evaBox.pub } }
    const share = await buildShare(await unshared(), lied, { agents: [], myComputers: [] })
    expect(share.by_jkt).toBe(owner.jkt)
  })

  it('re-seals the access token, keeping its owner_key and moving its floor', async () => {
    const state = await written(await unshared(), evaBox, { statement: statementOf({ version: 3 }) })
    const share = await withPins(state, { agents: [EVA], myComputers: [] })
    expect(sealedTargets(share.access_sealed as string)).toEqual(sealedTargets(share.sealed))
    const plain = await open(share.access_sealed as string, 'access', 'device', adaBox.jkt, adaBox.priv)
    expect(plain).toEqual(accessPlain({ statement_floor: 4 }))
    const refresh = await open(share.sealed, 'refresh', 'device', adaBox.jkt, adaBox.priv)
    expect(refresh).toEqual(refreshPlain({ statement_floor: 4 }))
    const sig = share.access_sig as Record<string, unknown>
    expect(sig.expect).toBe(await valueDigest(state.access_sealed))
    expect(await macOk(share.access_sealed as string, sig, 'access', 'device', adaBox.jkt, adaBox.priv)).toBe(true)
  })

  it('omits access when there is none', async () => {
    const share = await withPins(await unshared(), { agents: [EVA], myComputers: [] })
    expect('access_sealed' in share).toBe(false)
    expect('access_sig' in share).toBe(false)
  })

  it('omits an access not sealed to the owner', async () => {
    const state = await written(await unshared(), evaBox, { statement: statementOf({ version: 3 }), access: null })
    const access_sealed = await sealValue(JSON.stringify(accessPlain()), BINDING, 'access', 0, [toDevice(evaBox)])
    const share = await withPins({ ...state, access_sealed, access_sig: null } as ShareStateOut, { agents: [EVA], myComputers: [] })
    expect('access_sealed' in share).toBe(false)
  })

  it('omits an access the owner’s key cannot open', async () => {
    const state = await written(await unshared(), evaBox, { statement: statementOf({ version: 3 }) })
    // Authenticated by the machine, but sealed under the wrong key: nothing to re-seal.
    const recipients = [{ kind: 'member' as const, target: OWNER, publicJwk: stranger.pub }]
    const access_sealed = await sealValue(JSON.stringify(accessPlain()), BINDING, 'access', 0, recipients)
    const access_sig = await writerSig(BINDING, 'access', '', access_sealed,
      { kind: 'device', target: evaBox.jkt, privateKey: evaBox.priv, publicJwk: evaBox.pub },
      [{ kind: 'member', target: OWNER, publicJwk: owner.pub }])
    const share = await withPins({ ...state, access_sealed, access_sig } as ShareStateOut, { agents: [EVA], myComputers: [] })
    expect('access_sealed' in share).toBe(false)
  })

  it('stops when a pinned person’s key changed', async () => {
    const state = await unshared({ endorsements: [await vouch(stranger, ADA, EVA, adaBox)] })
    await refused(withPins(state, { agents: [EVA], myComputers: [] }), 'key-changed')
    // A change that touches none of the chosen agents stops nothing.
    await expect(withPins(state, { agents: [ECHO], myComputers: [] })).resolves.toBeTruthy()
  })

  it('re-shares over a writer pruned since it wrote, without reconnecting', async () => {
    // echo's box wrote last; the owner no longer administers echo, so it is no candidate
    // and echo is no agent to choose. The endorsement that vouches for it still comes.
    const base = await unshared({ agents: [{ id: EVA, slug: 'eva', name: 'Eva' }],
                                  candidates: [device(evaBox, EVA, 'eva-cloud')] })
    const state = await written(base, echoBox, { statement: statementOf({ agents: [EVA, ECHO].sort() }), to: [evaBox] })
    await refused(withPins({ ...state, endorsements: [] } as ShareStateOut, { agents: [EVA], myComputers: [] }), 'bad-writer')
    const withWriter = { ...state, endorsements: [await vouch(owner, OWNER, EVA, evaBox), await vouch(owner, OWNER, ECHO, echoBox)] }
    const share = await withPins(withWriter as ShareStateOut, { agents: [EVA], myComputers: [] })
    expect(sealedTargets(share.sealed)).toEqual([`device:${evaBox.jkt}`, `member:${OWNER}`].sort())
    expect((share.statement as unknown as Statement).version).toBe(4)
  })

  it('needs the private scalar to sign', async () => {
    await refused(buildShare(await unshared(), { ...me, scalar: undefined }, { agents: [], myComputers: [] }), 'no-scalar')
  })

  it('refuses rather than silently dropping a machine past the limit', async () => {
    const many = await Promise.all(Array.from({ length: 64 }, async (_, i) => {
      const d = await who((i + 16).toString(16))
      return device(d, '', `box-${i}`, true)
    }))
    const state = await unshared({ candidates: many })
    await refused(withPins(state, { agents: [], myComputers: many.map((c) => c.target) }), 'too-many')
  })

  it('will not sign over a statement below a floor', async () => {
    const state = await written(await unshared(), evaBox, {
      statement: statementOf({ version: 3 }), refresh: { statement_floor: 9 }, access: null })
    await refused(withPins(state), 'statement-below-floor')
  })
})

// ---- what this code writes, for the Python side ----------------------------------------

/** Two shares through fixed keys: the first after connect, then one over a machine's write. */
async function browserShareFixture() {
  const candidates = [
    { kind: 'member', target: OWNER, public_jwk: { ...owner.pub }, agent_id: '', runner_name: '', mine: true },
    device(evaBox, EVA, 'eva-cloud'),
    device(adaBox, EVA, 'ada-cloud'),
    device(strangerBox, EVA, 'stranger-cloud'),
    device(computer, '', 'jj-computer', true),
  ]
  const first = await unshared({ candidates, endorsements: [
    await vouch(owner, OWNER, EVA, evaBox), await vouch(ada, ADA, EVA, adaBox), await vouch(stranger, STRANGER, EVA, strangerBox),
  ], agents: [{ id: EVA, slug: 'eva', name: 'Eva' }] })
  const choice = { agents: [EVA], myComputers: [computer.jkt] }
  pinAda()
  const firstShare: ShareIn = await buildShare(first, me, choice)

  // The eva box refreshed: it re-sealed `refresh` and wrote `access` to the same recipients.
  const statement = firstShare.statement as unknown as Statement
  const everyone = [ownerRecipient(), toDevice(evaBox), toDevice(adaBox), toDevice(computer)]
  const by = { kind: 'device' as const, target: evaBox.jkt, privateKey: evaBox.priv, publicJwk: evaBox.pub }
  const sealed = await sealValue(JSON.stringify(refreshPlain({ statement_floor: 1 })), BINDING, 'refresh', 0, everyone)
  const access_sealed = await sealValue(JSON.stringify(accessPlain({ statement_floor: 1 })), BINDING, 'access', 0, everyone)
  const second = {
    ...first, state: 'active', statement, statement_sig: firstShare.sig, endorsers: statement.endorsers, sealed,
    sealed_sig: await writerSig(BINDING, 'refresh', await valueDigest(firstShare.sealed), sealed, by, everyone),
    sealed_digest: await valueDigest(sealed), access_sealed,
    access_sig: await writerSig(BINDING, 'access', '', access_sealed, by, everyone),
  } as unknown as ShareStateOut
  const secondShare = await buildShare(second, me, { agents: [EVA], myComputers: [] })

  const key = (w: Who) => ({ scalar_hex: Array.from(w.scalar, (b) => b.toString(16).padStart(2, '0')).join(''),
                             public_jwk: w.pub, jkt: w.jkt })
  return {
    format: 'canopy-connections/v1 browser share (written by frontend/src/vault/connectionShare.test.ts)',
    connection: CONNECTION, account_id: ACCOUNT, refresh_token: REFRESH_TOKEN, access_token: ACCESS_TOKEN,
    users: { owner: OWNER, ada: ADA, stranger: STRANGER }, agents: { eva: EVA },
    keys: { owner: key(owner), ada: key(ada), stranger: key(stranger), eva_box: key(evaBox), ada_box: key(adaBox),
            stranger_box: key(strangerBox), computer: key(computer) },
    first: { state: first, share: firstShare, msg: statementMessage(statement) },
    second: { state: second, share: secondShare, msg: statementMessage(secondShare.statement as unknown as Statement) },
  }
}

describe('the share the Python side checks', () => {
  type Written = Awaited<ReturnType<typeof browserShareFixture>>

  it.runIf(import.meta.env.VITE_WRITE_SHARE_FIXTURE)('writes the fixture', async () => {
    const fs = await import(/* @vite-ignore */ 'node:fs' as string)
    fs.writeFileSync('../docs/vault/zk-v1/connections_v1_browser_share.json',
      `${JSON.stringify(await browserShareFixture(), null, 2)}\n`)
  })

  it('this code still writes the checked-in shape', async () => {
    const fresh = await browserShareFixture()
    const held = JSON.parse(shareFixture) as Written
    const shapeOf = (x: Written) => JSON.stringify(x, (_k, val) =>
      typeof val === 'string' && (val.length > 40 || /^[A-Za-z0-9_-]{43}$/.test(val)) ? 'x' : val)
    expect(shapeOf(fresh)).toBe(shapeOf(held))
    expect(held.keys.owner.jkt).toBe(owner.jkt)
    for (const c of [held.first, held.second]) {
      const st = c.share.statement as unknown as Statement
      expect(statementMessage(st)).toBe(c.msg)
      expect(await verifyStatement(st, c.share.sig, owner.pub)).toBe(true)
      expect(await macOk(c.share.sealed, c.share.sealed_sig as Record<string, unknown>, 'refresh', 'member', OWNER, owner.priv)).toBe(true)
    }
  })
})
