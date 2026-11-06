/**
 * The owner's browser shares a connection: it opens the refresh token sealed to their
 * vault key, signs a grant statement and re-seals the token to the machines the statement
 * allows. canopy-web signs nothing, and opens an envelope only if the statement names it
 * (`canopy_web`, the "Project screens and apps" box), which only the owner can sign.
 *
 * The NORMATIVE definition of every byte is `docs/vault/zk-v1/connections_v1_reference.py`;
 * `connectionShare.test.ts` reproduces its vectors and writes the share that
 * `tests/test_connections_browser_share.py` puts through the real route and the reference.
 *
 * Everything in `ShareStateOut` is canopy-web's claim, so it is checked here:
 *  - a stored statement counts only under the owner's signature, whoever wrote the envelope:
 *    it sets the floor check, the next version and what callers show as today's choices;
 *  - an envelope is read only with its writer's MAC, from the owner's key or a machine the
 *    statement allows. The one exception is canopy-web's first seal, while `unshared` (R4);
 *  - a device is sealed to only when `vault/trust.ts` trusts an endorsement of its exact key
 *    for a chosen agent, or the owner chose it as their own (R14); the candidates only narrow;
 *  - the owner's key is the one the scalar in hand has, whatever the server named.
 *
 * Plaintext tokens stay inside this module: `openForOwner` answers only the account and
 * the verified statement. Read a stored statement from there, never from `state.statement`.
 */
import type { ShareIn, ShareStateOut } from '@/api/connections'
import type { ZkRecipientsOut } from '@/api/vault'
import { assess, loadPins, type Assessment, type Endorser, type Pins } from './trust'
import type { Unlocked } from './userKey'
import {
  b64u,
  ecdhZ,
  hkdf,
  importPublic,
  jkt,
  openSealedValue,
  publicJwk,
  publicJwkOfScalar,
  sealValue,
  SEALED_PREFIX,
  sha256,
  signWithScalar,
  unb64u,
  valueDigest,
  verifyEndorsement,
  verifySignature,
  bytesToHex,
  type Bytes,
  type PublicJwk,
  type Recipient,
  type SealedEntry,
} from './zk'

type Jwk = { x?: string; y?: string }
type Json = Record<string, unknown>
type Name = 'refresh' | 'access'

const enc = new TextEncoder()
const dec = new TextDecoder()

const STATEMENT_PREFIX = 'canopy-connections/v1/grant|'
const KV = 0
const MAX_LISTED = 64

const ACCOUNT_ID = /^[\x21\x23-\x5b\x5d-\x7e]{1,255}$/
const DECIMAL_ID = /^[1-9][0-9]*$/
const THUMBPRINT = /^[A-Za-z0-9_-]{43}$/
const DIGEST = /^[0-9a-f]{64}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const DNS_LABELS = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/
const LOGIN_HOSTS = ['login.salesforce.com', 'test.salesforce.com']
const MY_DOMAIN = '.my.salesforce.com'

const REFRESH_KEYS = ['account_id', 'client_id', 'instance_url', 'issued_at', 'login_host', 'owner_key', 'provider',
  'refresh_token', 'scopes', 'statement_floor', 'v']
const ACCESS_KEYS = ['access_token', 'account_id', 'expires_at', 'instance_url', 'owner_key', 'scope',
  'statement_floor', 'v']
const STATEMENT_KEYS = ['account_id', 'agents', 'connection', 'endorsers', 'my_computers', 'v', 'version']
/** `canopy_web` is optional: present only when the owner named canopy-web, never null or empty. */
const STATEMENT_KEYS_WITH_SERVER = [...STATEMENT_KEYS, 'canopy_web'].sort()

/** This browser will not read or write what it was handed. `code` says why; never a secret. */
export class ShareRefused extends Error {
  readonly code: string

  constructor(code: string, detail: string) {
    super(detail)
    this.name = 'ShareRefused'
    this.code = code
  }
}

export interface Statement {
  account_id: string
  agents: string[]
  connection: string
  endorsers: string[]
  my_computers: string[]
  v: 1
  version: number
  /** The thumbprint of the canopy-web key the owner lets use this connection. Absent when none. */
  canopy_web?: string
}

export interface OpenedAccount { account_id: string; instance_url: string; provider: string }
/** `canopyWeb`: the owner lets canopy-web use this connection itself, with nobody present. */
export interface ShareChoice { agents: string[]; myComputers: string[]; canopyWeb?: boolean }
export interface LeftOut { target: string; runner_name: string; agent_id: string; why: string }

/** A writer, for `writerSig`: only the owner's member key in production. */
export interface Writer { kind: 'device' | 'member'; target: string; privateKey: CryptoKey; publicJwk: Jwk }

interface OwnerKey { userId: string; publicJwk: PublicJwk; jkt: string }

/** Who this browser trusts for this connection's candidates: read once, then used to choose. */
export interface Trust {
  owner: OwnerKey
  /** Candidate keys whose thumbprint matched and that are points on the curve. */
  keys: Map<string, Jwk>
  /** Agent id → its `vault/trust.ts` verdict on that agent's machines. */
  assessments: Map<string, Assessment>
}

// ---- the grant statement (contract §0.3) ------------------------------------------------

const badStatement = (detail: string) => new ShareRefused('bad-statement', `The grant statement ${detail}.`)

function sortedUnique(items: unknown, pattern: RegExp, what: string): string[] {
  if (!Array.isArray(items) || items.length > MAX_LISTED) throw badStatement(`lists more than ${MAX_LISTED} ${what}`)
  if (!items.every((i) => typeof i === 'string' && pattern.test(i))) throw badStatement(`has a malformed entry in ${what}`)
  const want = [...new Set(items as string[])].sort()
  if (want.length !== items.length || want.some((x, i) => x !== items[i])) throw badStatement(`must keep ${what} sorted, once each`)
  return want
}

function checkedStatement(st: unknown): Statement {
  const s = st as Json
  const keys = s && typeof s === 'object' ? Object.keys(s).sort().join() : ''
  if (keys !== STATEMENT_KEYS.join() && keys !== STATEMENT_KEYS_WITH_SERVER.join()) throw badStatement('does not have exactly its fields')
  if ('canopy_web' in s && (typeof s.canopy_web !== 'string' || !THUMBPRINT.test(s.canopy_web))) {
    throw badStatement('names a malformed canopy-web key')
  }
  if (s.v !== 1) throw badStatement('is not v1')
  if (!Number.isSafeInteger(s.version) || (s.version as number) < 1) throw badStatement('has no valid version')
  if (typeof s.account_id !== 'string' || !ACCOUNT_ID.test(s.account_id)) throw badStatement('names a malformed account')
  if (typeof s.connection !== 'string' || !UUID.test(s.connection)) throw badStatement('names a malformed connection')
  return {
    account_id: s.account_id,
    agents: sortedUnique(s.agents, DECIMAL_ID, 'agents'),
    // Sorted between `agents` and `connection`, and only when named.
    ...('canopy_web' in s ? { canopy_web: s.canopy_web as string } : {}),
    connection: s.connection,
    endorsers: sortedUnique(s.endorsers, THUMBPRINT, 'endorsers'),
    my_computers: sortedUnique(s.my_computers, THUMBPRINT, 'computers'),
    v: 1,
    version: s.version as number,
  }
}

/** The text the owner signs. Keys in sorted order, and every value is ASCII, so this is
 *  byte for byte what Python's `json.dumps(sort_keys=True, ensure_ascii=True)` writes. */
export function statementMessage(st: Statement): string {
  return STATEMENT_PREFIX + JSON.stringify(checkedStatement(st))
}

export async function signStatement(st: Statement, scalar: Uint8Array, signerPub: Jwk): Promise<string> {
  return signWithScalar(scalar, signerPub, statementMessage(st))
}

export async function verifyStatement(st: Statement, sig: string, signerPub: Jwk): Promise<boolean> {
  return verifySignature(signerPub, sig, statementMessage(st))
}

// ---- writer MACs (contract §0.4) --------------------------------------------------------

/** `json.dumps(v, sort_keys=True, separators=(",", ":"))`: sorted keys, and non-ASCII escaped. */
function pythonJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(pythonJson).join(',')}]`
  if (value && typeof value === 'object') {
    const o = value as Json
    return `{${Object.keys(o).sort().map((k) => `${pythonJson(k)}:${pythonJson(o[k])}`).join(',')}}`
  }
  return JSON.stringify(value).replace(/[\u0080-￿]/g,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
}

export async function entryDigest(entry: SealedEntry): Promise<string> {
  return bytesToHex(await sha256(enc.encode(pythonJson(entry))))
}

export async function envelopeMsg(binding: string, name: string, expect: string, entry: SealedEntry): Promise<string> {
  return `canopy-connections/v1/envelope|${binding}|${name}|${expect}|${await entryDigest(entry)}`
}

export const envelopeMacInfo = (binding: string, name: string, kind: string, target: string) =>
  `canopy-connections/v1/envelope-mac|${binding}|${name}|${kind}:${target}`

async function macKey(binding: string, name: string, entry: SealedEntry, own: CryptoKey, peer: Jwk, usage: KeyUsage) {
  const z = await ecdhZ(own, peer)
  const raw = await hkdf(z, new Uint8Array(0), envelopeMacInfo(binding, name, entry.kind, entry.target))
  return globalThis.crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, [usage])
}

/** The MAC over one entry, keyed by ECDH(own static key, peer's): the writer computes it
 *  against the recipient's key, the recipient against the writer's. */
export async function envelopeMac(binding: string, name: string, expect: string, entry: SealedEntry,
                                  own: CryptoKey, peer: Jwk): Promise<string> {
  const key = await macKey(binding, name, entry, own, peer, 'sign')
  const msg = enc.encode(await envelopeMsg(binding, name, expect, entry))
  return b64u(new Uint8Array(await globalThis.crypto.subtle.sign('HMAC', key, msg)))
}

/** Constant time, by WebCrypto's own HMAC verify. */
async function macChecks(binding: string, name: string, expect: string, entry: SealedEntry, mac: string,
                         own: CryptoKey, peer: Jwk): Promise<boolean> {
  const key = await macKey(binding, name, entry, own, peer, 'verify')
  const msg = enc.encode(await envelopeMsg(binding, name, expect, entry))
  return globalThis.crypto.subtle.verify('HMAC', key, unb64u(mac) as Bytes, msg)
}

const badEnvelope = () => new ShareRefused('bad-envelope', 'This connection’s envelope is malformed.')

function sealedEntries(stored: string): SealedEntry[] {
  if (typeof stored !== 'string' || !stored.startsWith(SEALED_PREFIX)) throw badEnvelope()
  let body: { kv?: unknown; r?: unknown }
  try {
    body = JSON.parse(dec.decode(unb64u(stored.slice(SEALED_PREFIX.length))))
  } catch {
    throw badEnvelope()
  }
  if (body?.kv !== KV || !Array.isArray(body.r) || body.r.length < 1 || body.r.length > MAX_LISTED) throw badEnvelope()
  const seen = new Set<string>()
  for (const e of body.r as Json[]) {
    if (!e || typeof e !== 'object' || Object.keys(e).sort().join() !== 'ct,epk,kind,target'
        || typeof e.kind !== 'string' || typeof e.target !== 'string') throw badEnvelope()
    seen.add(`${e.kind}:${e.target}`)
  }
  if (seen.size !== body.r.length) throw badEnvelope()
  return body.r as SealedEntry[]
}

/** The sig stored beside an envelope: one MAC per entry, in entry order. `recipients` must
 *  hold every entry's key. */
export async function writerSig(binding: string, name: string, expect: string, stored: string, writer: Writer,
                                recipients: Recipient[]): Promise<Json> {
  const keys = new Map(recipients.map((r) => [`${r.kind}:${r.target}`, r.publicJwk]))
  const macs = []
  for (const e of sealedEntries(stored)) {
    const peer = keys.get(`${e.kind}:${e.target}`)
    if (!peer) throw badEnvelope()
    macs.push({ kind: e.kind, target: e.target, mac: await envelopeMac(binding, name, expect, e, writer.privateKey, peer) })
  }
  return { by: { kind: writer.kind, target: writer.target, public_jwk: publicJwk(writer.publicJwk) }, expect, macs }
}

// ---- reading what canopy-web hands over ---------------------------------------------------

const badSig = (detail: string) => new ShareRefused('bad-sig', `The writer’s signature ${detail}.`)

interface By { kind: string; target: string; public_jwk: Jwk }

async function checkedSig(sig: unknown, entries: SealedEntry[]): Promise<{ by: By; expect: string; macs: Map<string, string> }> {
  const s = sig as Json
  if (!s || typeof s !== 'object' || Object.keys(s).sort().join() !== 'by,expect,macs') throw badSig('is malformed')
  const by = s.by as By
  if (!by || typeof by !== 'object' || Object.keys(by).sort().join() !== 'kind,public_jwk,target'
      || typeof by.target !== 'string' || typeof by.kind !== 'string') throw badSig('names its writer badly')
  const jwk = by.public_jwk as Json
  if (!jwk || typeof jwk !== 'object' || Object.keys(jwk).sort().join() !== 'crv,kty,x,y'
      || jwk.kty !== 'EC' || jwk.crv !== 'P-256') throw badSig('does not carry exactly a P-256 public key')
  try {
    await importPublic(by.public_jwk)
  } catch {
    throw badSig('carries a key that is not on P-256')
  }
  if (typeof s.expect !== 'string' || !(s.expect === '' || DIGEST.test(s.expect))) throw badSig('names a malformed digest')
  if (!Array.isArray(s.macs)) throw badSig('has no MAC list')
  const macs = new Map<string, string>()
  for (const m of s.macs as Json[]) {
    if (!m || typeof m !== 'object' || Object.keys(m).sort().join() !== 'kind,mac,target'
        || typeof m.mac !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(m.mac)) throw badSig('has a malformed MAC')
    macs.set(`${m.kind}:${m.target}`, m.mac)
  }
  const want = entries.map((e) => `${e.kind}:${e.target}`)
  if (macs.size !== s.macs.length || macs.size !== want.length || !want.every((k) => macs.has(k))) {
    throw badSig('does not cover exactly this envelope’s recipients')
  }
  return { by, expect: s.expect, macs }
}

/** A device the stored statement allows: one of `my_computers`, or an endorsement of its
 *  exact key for an agent it lists, by the owner or someone it names. The statement says
 *  who may vouch here, not this browser's pins: this is the check every machine makes.
 *  `stored` has passed `storedStatement`, so it is the owner's own word. */
async function deviceAllowed(thumb: string, state: ShareStateOut, stored: Statement | null, ownerJkt: string): Promise<boolean> {
  if (!stored) return false
  if (stored.canopy_web === thumb) return true
  if (stored.my_computers.includes(thumb)) return true
  const vouchers = new Set([ownerJkt, ...stored.endorsers])
  for (const e of state.endorsements ?? []) {
    if (e.kind !== 'device' || e.target !== thumb || e.key_jkt !== thumb || !e.by_public_jwk) continue
    if (!stored.agents.includes(String(e.agent_id))) continue
    const signer = e.by_public_jwk as Jwk
    if (vouchers.has(await jkt(signer)) && await verifyEndorsement(e, String(e.agent_id), signer)) return true
  }
  return false
}

/** While the row is `unshared` the owner has signed nothing yet, so a device writer is accepted
 *  on its MAC alone (`authenticate` checked it by the writer's own key), as the unsigned first seal
 *  is: canopy-web's seal after sign-in, whether or not it can report its key just now. Once shared,
 *  only a statement the owner signed that names the writer admits it (`deviceAllowed`);
 *  `server.thumbprint` never does. */
const whileUnshared = (state: ShareStateOut): boolean => state.state === 'unshared'

/** Refuse unless `sig` proves who wrote this recipient's entry, and they may have. */
async function authenticate(state: ShareStateOut, stored: Statement | null, owner: OwnerKey, me: Unlocked, name: Name,
                            entries: SealedEntry[], entry: SealedEntry, sig: unknown): Promise<void> {
  const { by, expect, macs } = await checkedSig(sig, entries)
  const thumb = await jkt(by.public_jwk)
  if (by.kind === 'device' ? by.target !== thumb : !(by.kind === 'member' && DECIMAL_ID.test(by.target))) {
    throw badSig('names its writer by the wrong key')
  }
  const mac = macs.get(`${entry.kind}:${entry.target}`) as string
  if (!(await macChecks(state.binding, name, expect, entry, mac, me.privateKey, by.public_jwk))) {
    throw new ShareRefused('bad-mac', 'Whoever wrote this connection’s envelope cannot be proven.')
  }
  const allowed = by.kind === 'member'
    ? by.target === owner.userId && thumb === owner.jkt
    : whileUnshared(state) || await deviceAllowed(thumb, state, stored, owner.jkt)
  if (!allowed) throw new ShareRefused('bad-writer', 'This connection’s envelope was written by someone it does not allow.')
}

function myDomain(host: string): boolean {
  return host.endsWith(MY_DOMAIN) && host.length <= 253 && DNS_LABELS.test(host.slice(0, -MY_DOMAIN.length))
}

function instanceOk(url: string): boolean {
  return url.startsWith('https://') && myDomain(url.slice('https://'.length).replace(/\/$/, ''))
}

function checkHosts(provider: unknown, loginHost: unknown, instanceUrl: unknown): void {
  const ok = provider === 'salesforce'
    ? typeof loginHost === 'string' && (LOGIN_HOSTS.includes(loginHost) || myDomain(loginHost))
      && typeof instanceUrl === 'string' && instanceOk(instanceUrl)
    // A provider with no hosts of its own (Google): both are "" in the plaintext.
    : loginHost === '' && instanceUrl === ''
  if (!ok) throw new ShareRefused('bad-host', 'This connection names a host its provider does not use.')
}

const badPlaintext = (name: Name) => new ShareRefused('bad-plaintext', `The ${name} envelope does not hold what it should.`)

function parsed(text: string, name: Name): Json {
  try {
    const plain = JSON.parse(text) as Json
    if (plain && typeof plain === 'object') return plain
  } catch {
    // reported below, without the text
  }
  throw badPlaintext(name)
}

function checkFloor(plain: Json, name: Name, stored: Statement | null): void {
  if (!Number.isSafeInteger(plain.statement_floor) || (plain.statement_floor as number) < 0) throw badPlaintext(name)
  if ((stored?.version ?? 0) < (plain.statement_floor as number)) {
    throw new ShareRefused('statement-below-floor',
      `This connection’s stored statement is older than the ${name} envelope allows: canopy-web may be replaying an old one.`)
  }
}

interface Opened { owner: OwnerKey; stored: Statement | null; refresh: Json; access: Json | null }

/** The stored statement, once it is the owner's: about this connection and account, and
 *  signed by the key in hand. `readRefresh` then refuses any envelope whose sealed
 *  `owner_key` is not that key's thumbprint, and nothing leaves `readAll` before both pass.
 *  A real row always has both: a share writes them together and a reconnect leaves them. */
async function storedStatement(state: ShareStateOut, owner: OwnerKey): Promise<Statement | null> {
  if (!state.statement) return null
  const st = checkedStatement(state.statement)
  if (st.connection !== state.id) throw badStatement('names another connection')
  if (st.account_id !== state.account_id) throw badStatement('names another account')
  if (!state.statement_sig || !(await verifyStatement(st, state.statement_sig, owner.publicJwk))) {
    throw new ShareRefused('bad-statement-sig', 'You did not sign the grant statement canopy-web shows for this connection.')
  }
  return st
}

async function readRefresh(state: ShareStateOut, stored: Statement | null, owner: OwnerKey, me: Unlocked): Promise<Json> {
  const entries = sealedEntries(state.sealed)
  const entry = entries.find((e) => e.kind === 'member' && e.target === owner.userId)
  if (!entry) throw new ShareRefused('not-for-me', 'This connection is not sealed to your vault key.')
  if (state.sealed_sig) {
    await authenticate(state, stored, owner, me, 'refresh', entries, entry, state.sealed_sig)
  } else if (state.state !== 'unshared') {
    throw new ShareRefused('unsigned', 'Nobody vouches for this connection’s envelope, and it is already shared.')
  }
  let text: string
  try {
    text = await openSealedValue(state.sealed, state.binding, 'refresh', 'member', owner.userId, me.privateKey)
  } catch {
    throw new ShareRefused('cannot-open', 'Your vault key does not open this connection.')
  }
  const plain = parsed(text, 'refresh')
  if (Object.keys(plain).sort().join() !== REFRESH_KEYS.join() || plain.v !== 1) throw badPlaintext('refresh')
  if (plain.provider !== state.provider) throw badPlaintext('refresh')
  if (typeof plain.refresh_token !== 'string' || !plain.refresh_token
      || typeof plain.client_id !== 'string' || !plain.client_id) throw badPlaintext('refresh')
  checkHosts(plain.provider, plain.login_host, plain.instance_url)
  if (plain.owner_key !== owner.jkt) throw new ShareRefused('bad-owner-key', 'This connection names another key as its owner.')
  if (plain.account_id !== state.account_id) throw new ShareRefused('account-mismatch', 'This connection opened to another account.')
  checkFloor(plain, 'refresh', stored)
  return plain
}

/** The opened `access`, or null when there is none for the owner to re-seal. What the owner
 *  cannot open is left out of a share; what they can, but that does not belong, is refused. */
async function readAccess(state: ShareStateOut, stored: Statement | null, owner: OwnerKey, me: Unlocked,
                          refresh: Json): Promise<Json | null> {
  if (!state.access_sealed) return null
  let entries: SealedEntry[]
  try {
    entries = sealedEntries(state.access_sealed)
  } catch {
    return null
  }
  const entry = entries.find((e) => e.kind === 'member' && e.target === owner.userId)
  if (!entry) return null
  if (!state.access_sig) throw new ShareRefused('unsigned', 'Nobody vouches for this connection’s access envelope.')
  await authenticate(state, stored, owner, me, 'access', entries, entry, state.access_sig)
  let text: string
  try {
    text = await openSealedValue(state.access_sealed, state.binding, 'access', 'member', owner.userId, me.privateKey)
  } catch {
    return null
  }
  const plain = parsed(text, 'access')
  if (Object.keys(plain).sort().join() !== ACCESS_KEYS.join() || plain.v !== 1) throw badPlaintext('access')
  if (typeof plain.access_token !== 'string' || !plain.access_token || !Number.isSafeInteger(plain.expires_at)) throw badPlaintext('access')
  if (plain.owner_key !== refresh.owner_key || plain.account_id !== refresh.account_id) {
    throw new ShareRefused('access-mismatch', 'This connection’s access envelope belongs to another owner or account.')
  }
  checkHosts(refresh.provider, refresh.login_host, plain.instance_url)
  checkFloor(plain, 'access', stored)
  return plain
}

/** The owner's key as the scalar in hand has it; the server's copy is only compared. */
async function ownerOf(state: ShareStateOut, me: Unlocked): Promise<OwnerKey> {
  let pub: PublicJwk
  try {
    pub = me.scalar ? await publicJwkOfScalar(me.scalar, me.publicJwk) : publicJwk(me.publicJwk)
  } catch {
    throw new ShareRefused('bad-owner-key', 'Your vault key’s public half does not match it.')
  }
  const thumb = await jkt(pub)
  if (state.owner.user_id !== me.userId || (await jkt(state.owner.public_jwk as Jwk)) !== thumb) {
    throw new ShareRefused('bad-owner-key', 'canopy-web names another key as this connection’s owner.')
  }
  return { userId: me.userId, publicJwk: pub, jkt: thumb }
}

async function readAll(state: ShareStateOut, me: Unlocked): Promise<Opened> {
  const owner = await ownerOf(state, me)
  if (!UUID.test(state.id) || state.binding !== `connection:${state.id}`) {
    throw new ShareRefused('bad-binding', 'This connection’s binding is not its own.')
  }
  const stored = await storedStatement(state, owner)
  const refresh = await readRefresh(state, stored, owner, me)
  return { owner, stored, refresh, access: await readAccess(state, stored, owner, me, refresh) }
}

/** Open the connection enough to say which account it is and what its owner last signed
 *  (null before the first share). The token stays in here: `buildShare` opens it again
 *  with the same unlocked key. */
export async function openForOwner(state: ShareStateOut, me: Unlocked):
    Promise<{ account: OpenedAccount; statement: Statement | null }> {
  const { refresh, stored } = await readAll(state, me)
  return { account: { account_id: refresh.account_id as string, instance_url: refresh.instance_url as string,
                      provider: refresh.provider as string },
           statement: stored }
}

// ---- who may read it (contract §0.5, R14) ------------------------------------------------

async function keyMatches(target: string, jwk: Jwk): Promise<boolean> {
  try {
    await importPublic(jwk)
    return (await jkt(jwk)) === target
  } catch {
    return false
  }
}

/** Read every candidate's key and every agent's endorsements through `vault/trust.ts`, for
 *  the page to show and `buildShare` to seal by. `pins`: this browser's, unless a test says otherwise. */
export async function trustFor(state: ShareStateOut, me: Unlocked, opts: { pins?: Pins; accept?: Set<string> } = {}): Promise<Trust> {
  const owner = await ownerOf(state, me)
  const keys = new Map<string, Jwk>()
  const byAgent = new Map<string, ZkRecipientsOut['recipients'][number][]>()
  for (const c of state.candidates) {
    if (c.kind !== 'device' || !(await keyMatches(c.target, c.public_jwk as Jwk))) continue
    keys.set(c.target, c.public_jwk as Jwk)
    if (!c.agent_id) continue
    const rows = byAgent.get(c.agent_id) ?? []
    rows.push({ kind: 'device', target: c.target, label: c.runner_name, public_jwk: c.public_jwk, key_jkt: c.target } as never)
    byAgent.set(c.agent_id, rows)
  }
  const pins = opts.pins ?? loadPins()
  const assessments = new Map<string, Assessment>()
  for (const a of state.agents) {
    const data = { agent: a.slug, agent_id: a.id, kv: KV, recipients: byAgent.get(a.id) ?? [],
                   endorsements: (state.endorsements ?? []).filter((e) => e.agent_id === a.id), declared: [], stored: [] }
    assessments.set(a.id, await assess(data as unknown as ZkRecipientsOut,
      { self: { userId: owner.userId, publicJwk: owner.publicJwk }, pins, accept: opts.accept }))
  }
  return { owner, keys, assessments }
}

interface Decision { sealTo: Map<string, Recipient>; computers: string[]; agentDevices: Set<string>; leftOut: Map<string, LeftOut> }

function decide(state: ShareStateOut, choice: ShareChoice, trust: Trust): Decision {
  const agents = new Set(choice.agents)
  const computers = new Set(choice.myComputers)
  const sealTo = new Map<string, Recipient>()
  const mine = new Set<string>()
  const agentDevices = new Set<string>()
  const out = new Map<string, LeftOut>()
  const offered = new Set<string>()
  for (const c of state.candidates) {
    if (c.kind !== 'device') continue
    const own = !c.agent_id && computers.has(c.target)
    const lent = !!c.agent_id && agents.has(c.agent_id)
    if (!own && !lent) continue
    if (own) offered.add(c.target)
    const key = trust.keys.get(c.target)
    const vouched = lent && !!trust.assessments.get(c.agent_id)?.sealTo.some((r) => r.target === c.target)
    const why = !key ? 'its key does not match its thumbprint'
      : own && !c.mine ? 'it is not one of your computers'
        : lent && !vouched ? 'not vouched for by anyone you trust'
          : ''
    if (why || !key) {
      out.set(c.target, { target: c.target, runner_name: c.runner_name, agent_id: c.agent_id, why })
      continue
    }
    sealTo.set(c.target, { kind: 'device', target: c.target, publicJwk: key })
    if (own) mine.add(c.target)
    else agentDevices.add(c.target)
  }
  for (const target of computers) {
    if (!offered.has(target)) out.set(target, { target, runner_name: '', agent_id: '', why: 'not one of your enrolled computers' })
  }
  for (const target of sealTo.keys()) out.delete(target)
  return { sealTo, computers: [...mine], agentDevices, leftOut: out }
}

/** Who a share with `choice` is sealed to besides canopy-web: the owner, and each chosen machine
 *  this browser will vouch for. Why the rest were left out is `leftOut`. `choice.canopyWeb` is not
 *  read here: `buildShare` adds canopy-web (`serverRecipient`) to both envelopes when it is ticked. */
export function chooseRecipients(state: ShareStateOut, choice: ShareChoice, trust: Trust): Recipient[] {
  const owner: Recipient = { kind: 'member', target: trust.owner.userId, publicJwk: trust.owner.publicJwk }
  return [owner, ...decide(state, choice, trust).sealTo.values()]
}

/** The chosen machines `chooseRecipients` will not seal to, each named with why. */
export function leftOut(state: ShareStateOut, choice: ShareChoice, trust: Trust): LeftOut[] {
  return [...decide(state, choice, trust).leftOut.values()]
}

/** A person whose key changed under a chosen agent: what a substituted key looks like. */
export function changedEndorser(trust: Trust, choice: ShareChoice): Endorser | undefined {
  for (const a of choice.agents) {
    const hit = trust.assessments.get(a)?.endorsers.find((e) => e.status === 'changed')
    if (hit) return hit
  }
  return undefined
}

/** People this browser has not decided about, vouching under a chosen agent for a machine
 *  nobody it trusts vouches for: what stands between the owner and that machine (R14). Each
 *  once. The owner accepts one by name and fingerprint; `vault/trust.ts` pins them. */
export function vouchersToDecide(trust: Trust, choice: ShareChoice): Endorser[] {
  const out = new Map<string, Endorser>()
  for (const a of choice.agents) {
    const verdict = trust.assessments.get(a)
    const trusted = new Set(verdict?.sealTo.map((r) => `${r.kind}:${r.target}`))
    for (const e of verdict?.endorsers ?? []) {
      if (e.status === 'new' && e.covers.some((c) => !trusted.has(c))) out.set(e.by, e)
    }
  }
  return [...out.values()]
}

/** The people, besides the owner, whose endorsements this browser trusts for the machines
 *  it is sealing to. Nobody else: the statement is what every machine will believe. */
function trustedEndorsers(choice: ShareChoice, trust: Trust, sealedTo: Set<string>): string[] {
  const out = new Set<string>()
  for (const a of choice.agents) {
    for (const e of trust.assessments.get(a)?.endorsers ?? []) {
      if (e.status !== 'pinned' && e.status !== 'accepted') continue
      if (e.jkt !== trust.owner.jkt && e.covers.some((c) => sealedTo.has(c.slice('device:'.length)))) out.add(e.jkt)
    }
  }
  return [...out].sort()
}

/** canopy-web as a recipient, when the owner ticked it. The key is canopy-web's claim, so its
 *  thumbprint is computed here and must be the one it reported; a statement never names it
 *  otherwise. Null when the owner did not tick the box. */
async function serverRecipient(state: ShareStateOut, choice: ShareChoice): Promise<Recipient | null> {
  if (!choice.canopyWeb) return null
  const sv = state.server
  if (!sv?.available || !sv.provider_calls || !sv.statements_enabled || !sv.public_jwk || !THUMBPRINT.test(sv.thumbprint)) {
    throw new ShareRefused('server-unavailable', 'canopy-web cannot use this connection for you right now: nothing was sent.')
  }
  const key = sv.public_jwk as Jwk
  if (!(await keyMatches(sv.thumbprint, key))) {
    throw new ShareRefused('server-key', 'The key canopy-web gave does not match its fingerprint: nothing was sent.')
  }
  return { kind: 'device', target: sv.thumbprint, publicJwk: key as PublicJwk }
}

// ---- the share ---------------------------------------------------------------------------

/** Everything `PUT /api/connections/{id}/share` needs, for `choice`, and the chosen machines
 *  it did not seal to. `trust` is the one the page showed (`trustFor`), so what is signed
 *  is never wider than what the owner saw. Needs the private scalar: only a key opened in
 *  this flow signs. */
export async function buildShare(state: ShareStateOut, me: Unlocked, choice: ShareChoice,
                                 trust: Trust): Promise<{ share: ShareIn; leftOut: LeftOut[] }> {
  if (!me.scalar) throw new ShareRefused('no-scalar', 'Unlock your vault key to share this connection.')
  const { owner, stored, refresh, access } = await readAll(state, me)
  if (trust.owner.jkt !== owner.jkt) throw new ShareRefused('bad-owner-key', 'This list of machines was read for another vault key.')
  const changed = changedEndorser(trust, choice)
  if (changed) {
    throw new ShareRefused('key-changed', `${changed.email || 'Someone'}’s vault key changed since this browser last `
      + 'trusted it. Confirm with them before sharing: nothing was sent.')
  }
  const { sealTo, computers, agentDevices, leftOut: dropped } = decide(state, choice, trust)
  const server = await serverRecipient(state, choice)
  const recipients = [{ kind: 'member' as const, target: owner.userId, publicJwk: owner.publicJwk }, ...sealTo.values(),
                      ...(server && !sealTo.has(server.target) ? [server] : [])]
  if (recipients.length > MAX_LISTED) throw new ShareRefused('too-many', `A connection can reach at most ${MAX_LISTED} recipients.`)

  const version = (stored?.version ?? 0) + 1
  const statement = checkedStatement({
    account_id: refresh.account_id,
    agents: [...new Set(choice.agents)].sort(),
    ...(server ? { canopy_web: server.target } : {}),
    connection: state.id,
    endorsers: trustedEndorsers(choice, trust, agentDevices),
    my_computers: [...computers].sort(),
    v: 1,
    version,
  })
  const writer: Writer = { kind: 'member', target: owner.userId, privateKey: me.privateKey, publicJwk: owner.publicJwk }
  const seal = async (plain: Json, name: Name, expect: string) => {
    const value = await sealValue(JSON.stringify({ ...plain, statement_floor: version }), state.binding, name, KV, recipients)
    return { value, sig: await writerSig(state.binding, name, expect, value, writer, recipients) }
  }
  const sealed = await seal(refresh, 'refresh', state.sealed_digest)
  const share: ShareIn = {
    statement: statement as unknown as ShareIn['statement'],
    sig: await signStatement(statement, me.scalar, owner.publicJwk),
    by_jkt: owner.jkt,
    sealed: sealed.value,
    sealed_sig: sealed.sig,
    expect: state.sealed_digest,
  }
  const left = [...dropped.values()]
  if (!access) return { share, leftOut: left }
  const again = await seal(access, 'access', await valueDigest(state.access_sealed))
  return { share: { ...share, access_sealed: again.value, access_sig: again.sig }, leftOut: left }
}
