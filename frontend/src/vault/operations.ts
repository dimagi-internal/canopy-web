/**
 * The zero-knowledge vault flows (canopy-web#1550), composed from the crypto
 * module (`./zk`), the passkey PRF ceremony (`./prf`) and the REST calls
 * (`@/api/vault`). Every secret here — the vault key VK, a person's private
 * scalar, a recovery secret, a decrypted value — lives only in local variables
 * of one call and is never sent anywhere; what goes to canopy-web is
 * ciphertext, wraps and seals. There is no way back to custodial storage:
 * zero-knowledge is the canopy-web vault's only mode (Jonathan, 2026-10-17).
 */
import { revealAgentCredential } from '@/api/agents'
import { approveRunnerDeviceKey, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { registerThisDevice, type PasskeyOut } from '@/api/passkeys'
import { defaultLabel } from '@/auth/webauthn'
import {
  addZkEndorsements,
  addZkWraps,
  enableZk,
  fulfilSecretRequest,
  getKeyset,
  getZkState,
  putKeyset,
  putZkValues,
  rotateZk,
  type VaultKeysetOut,
  type ZkStateOut,
  type ZkTargetOut,
  type ZkWrapIn,
  type ZkWrapOut,
} from '@/api/vault'
import { assertWithPrf, PrfUnsupportedError } from './prf'
import type { Assessment } from './trust'
import {
  decryptValue,
  deviceInfo,
  encryptValue,
  endorse,
  generateKeyPair,
  isSealed,
  openSealedValue,
  sealValue,
  valueDigest,
  type Recipient,
  importPrivateScalar,
  memberInfo,
  openUserKey,
  openUserKey2skd,
  openUserKeyRecovery,
  parseRecoveryCode,
  parseSecretKeyCode,
  passphraseProblem,
  randomBytes,
  recoveryCode,
  sealUserKey,
  sealUserKey2skd,
  sealUserKeyRecovery,
  secretKeyCode,
  unwrapEcdhEs,
  unwrapRecovery,
  wrapEcdhEs,
  wrapRecovery,
  type AnySeal,
  type EcdhWrap,
  type PassphraseSeal,
  type PersonRecoverySeal,
  type PublicJwk,
  type RecoveryWrap,
  type UserKeySeal,
} from './zk'

type Jwk = { x?: string; y?: string }

/** A person's opened keyset: their private key, for one flow. */
export interface Unlocked {
  userId: string
  privateKey: CryptoKey
  publicJwk: Jwk
  /** The private scalar, when this flow opened it — to add a seal (the
   *  person's recovery code) without asking again. Never sent anywhere. */
  scalar?: Uint8Array
}

// ---- the person's vault key -----------------------------------------------------------
//
// A keyset holds one or more SEALS of the private key: a passkey's PRF output
// (`prf`), or a passphrase + Secret Key (`2skd`, 1Password-style). The Secret
// Key is remembered in this browser (localStorage, per user) so unlocking here
// needs only the passphrase; on a new device the person types both.
//
// Deliberate, and why CodeQL's clear-text-storage alert (#39) is dismissed: the
// Secret Key alone opens nothing — it is one of TWO factors, mixed with the
// passphrase (which is never stored) in 2SKD. Its job is to make a stolen
// copy of canopy-web's database un-guessable, not to resist this browser's own
// storage; 1Password keeps it on the device the same way.

const SK_STORE = (userId: string) => `canopy-vault-secret-key:${userId}`

export function rememberedSecretKey(userId: string): string | null {
  try {
    return window.localStorage.getItem(SK_STORE(userId))
  } catch {
    return null
  }
}

function rememberSecretKey(userId: string, code: string) {
  try {
    window.localStorage.setItem(SK_STORE(userId), code)
  } catch {
    // private window / storage blocked: the person types it next time
  }
}

/** How the person chose to unlock. */
export type UnlockChoice =
  | { method: 'passkey' }
  | { method: 'passphrase'; passphrase: string; secretKey?: string }

/** What the person can unlock with, for a prompt to offer. */
export interface UnlockAsk {
  passkey: boolean
  passphrase: boolean
  /** No Secret Key is remembered in this browser — ask for it too. */
  needsSecretKey: boolean
}

export type UnlockPrompt = (ask: UnlockAsk) => Promise<UnlockChoice>
export type Unlock = () => Promise<Unlocked>

const prfSeals = (ks: VaultKeysetOut) => ((ks.seals ?? []) as unknown as AnySeal[]).filter((s): s is UserKeySeal => s.alg === 'prf')
const passSeals = (ks: VaultKeysetOut) =>
  ((ks.seals ?? []) as unknown as AnySeal[]).filter((s): s is PassphraseSeal => s.alg === '2skd')

async function openScalar(ks: VaultKeysetOut, prompt: UnlockPrompt): Promise<Uint8Array> {
  const byPasskey = prfSeals(ks), byPass = passSeals(ks)
  const ask: UnlockAsk = { passkey: byPasskey.length > 0, passphrase: byPass.length > 0,
                           needsSecretKey: !rememberedSecretKey(ks.user_id) }
  const choice: UnlockChoice = ask.passkey && !ask.passphrase ? { method: 'passkey' } : await prompt(ask)
  if (choice.method === 'passkey') {
    const touch = await assertWithPrf(ks.prf_input, { allow: byPasskey.map((s) => s.cred) })
    const seal = byPasskey.find((s) => s.cred === touch.cred)
    if (!seal) throw new Error('That passkey does not hold your vault key — use one marked ✓ under Settings → Security.')
    if (!touch.prf) throw new PrfUnsupportedError()
    return openUserKey(seal, ks.user_id, touch.prf)
  }
  const code = choice.secretKey?.trim() || rememberedSecretKey(ks.user_id)
  if (!code) throw new Error('Enter your Secret Key (it starts with CV1-, on your Emergency Kit).')
  const sk = parseSecretKeyCode(code)
  for (const seal of byPass) {
    try {
      const scalar = await openUserKey2skd(seal, ks.user_id, choice.passphrase, sk)
      if (choice.secretKey) rememberSecretKey(ks.user_id, secretKeyCode(sk))
      return scalar
    } catch {
      // the next passphrase seal, if any
    }
  }
  throw new Error('That passphrase and Secret Key do not open your vault key.')
}

/** The ONE passkey touch that both steps up (the server's five-minute window)
 *  and opens the keyset through PRF — or null when the keyset has no passkey
 *  seal (the caller then steps up as usual and asks for the passphrase). */
export async function stepUpAndUnlock(ks: VaultKeysetOut): Promise<Unlocked | null> {
  const byPasskey = prfSeals(ks)
  if (!ks.exists || !ks.public_jwk || !byPasskey.length) return null
  const touch = await assertWithPrf(ks.prf_input, { allow: byPasskey.map((s) => s.cred) })
  const seal = byPasskey.find((s) => s.cred === touch.cred)
  if (!seal || !touch.prf) return null
  const pub = ks.public_jwk as Jwk
  const scalar = await openUserKey(seal, ks.user_id, touch.prf)
  return { userId: ks.user_id, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
}

/** Open the keyset — passkey or passphrase, as the person chooses. */
export async function unlockVaultKey(prompt: UnlockPrompt, ks?: VaultKeysetOut): Promise<Unlocked> {
  ks = ks ?? (await getKeyset())
  if (!ks.exists || !ks.public_jwk) throw new Error('Set up your vault key first (Settings → Vault key).')
  const scalar = await openScalar(ks, prompt)
  const pub = ks.public_jwk as Jwk
  return { userId: ks.user_id, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
}

/** Whether a passkey can plausibly hold the vault key here: the browser does
 *  not rule PRF out and some passkey did not report it missing at
 *  registration. Many passkey apps (1Password's extension among them) give no
 *  PRF, and for those a passphrase is the way — so a page offers the passkey
 *  route only when this is true, and still falls back if the touch says no. */
export function passkeyCanHoldVaultKey(passkeys: readonly { prf_enabled?: boolean | null }[], browser: boolean | null): boolean {
  return browser !== false && passkeys.some((p) => p.prf_enabled === true)
}

/** How a person without a vault key should get one:
 *  - `passkey`: a passkey of theirs reported PRF — one touch makes the key;
 *  - `upgrade`: none did, but a new passkey may (one registered before PRF was
 *    asked for reports `null`, and a password manager such as 1Password then
 *    made it without PRF) — register a replacement, then use it;
 *  - `passphrase`: this browser has no PRF, or every passkey said no. */
export type VaultKeyRoute = 'passkey' | 'upgrade' | 'passphrase'

export function vaultKeyRoute(passkeys: readonly { prf_enabled?: boolean | null }[], browser: boolean | null): VaultKeyRoute {
  if (passkeyCanHoldVaultKey(passkeys, browser)) return 'passkey'
  if (browser === false) return 'passphrase'
  return passkeys.length === 0 || passkeys.some((p) => p.prf_enabled !== false) ? 'upgrade' : 'passphrase'
}

/** Register a replacement passkey that can hold a vault key (same label as the
 *  one it replaces). Returns it; its `prf_enabled` says whether it can. The
 *  old passkey stays until the person deletes it. */
export async function upgradePasskey(label: string): Promise<PasskeyOut> {
  return registerThisDevice(label, { upgrade: true })
}

async function unlockedFrom(scalar: Uint8Array, userId: string, publicJwk: PublicJwk): Promise<Unlocked> {
  const pub = publicJwk as Jwk
  return { userId, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
}

// ---- the person's ONE recovery code (§4c) ---------------------------------------------------
//
// One code per person opens their key, and through it every agent they hold —
// instead of a code per agent. It is a seal like the passkey's and the
// passphrase's, made here from the open scalar; canopy-web never sees the code.

const sealsOf = (ks: VaultKeysetOut) => (ks.seals ?? []) as unknown as AnySeal[]

export function hasRecoveryCode(ks: VaultKeysetOut | null | undefined): boolean {
  return Boolean(ks?.exists) && sealsOf(ks!).some((s) => s.alg === 'recovery')
}

/** A recovery code for the person, ready but not yet stored: show `code`
 *  ONCE, then `commit` stores its seal (replacing any earlier one). */
export interface PreparedRecovery {
  code: string
  commit: () => Promise<VaultKeysetOut>
}

export async function prepareRecoveryCode(me: Unlocked): Promise<PreparedRecovery> {
  if (!me.scalar) throw new Error('Unlock your vault key again to make a recovery code.')
  const r = randomBytes(32)
  const seal = await sealUserKeyRecovery(me.scalar, me.userId, r)
  return {
    code: recoveryCode(r),
    commit: async () => {
      const ks = await getKeyset()
      const keep = sealsOf(ks).filter((s) => s.alg !== 'recovery')
      return putKeyset({ public_jwk: ks.public_jwk as Record<string, unknown>,
                         seals: [...keep, seal] as unknown as Record<string, unknown>[] })
    },
  }
}

/** Before a flow that would hand out a per-agent code: make sure the person
 *  has their ONE recovery code instead — shown via `confirm` (which resolves
 *  once they kept it) and stored. A no-op when they already have one, or when
 *  this unlock did not open the scalar (the flow then falls back to a code for
 *  that agent). */
export async function ensureRecoveryCode(me: Unlocked, confirm: (code: string) => Promise<void>): Promise<void> {
  if (!me.scalar || hasRecoveryCode(await getKeyset())) return
  const prepared = await prepareRecoveryCode(me)
  await confirm(prepared.code)
  await prepared.commit()
}

/**
 * Does this recovery code open your key? Opened here, in the browser, and then
 * thrown away — nothing is saved or sent. `'malformed'` is a typo the parser
 * can name (`detail`); `'wrong'` is a well-formed code for some other key.
 */
export async function checkRecoveryCode(code: string):
    Promise<{ result: 'works' | 'wrong' | 'malformed' | 'none'; detail?: string }> {
  let r: Uint8Array
  try {
    r = parseRecoveryCode(code)
  } catch (e) {
    return { result: 'malformed', detail: e instanceof Error ? e.message : '' }
  }
  const ks = await getKeyset()
  const seal = sealsOf(ks).find((s): s is PersonRecoverySeal => s.alg === 'recovery')
  if (!ks.exists || !seal) return { result: 'none' }
  try {
    const scalar = await openUserKeyRecovery(seal, ks.user_id, r)
    scalar.fill(0)
    return { result: 'works' }
  } catch {
    return { result: 'wrong' }
  }
}

/**
 * Lost every passkey (and the passphrase)? The recovery code opens the key;
 * it is then sealed again — under a passkey touched now (which also steps up),
 * or under a new passphrase + Secret Key (the caller must have stepped up, e.g.
 * by registering a new passkey). Returns the Secret Key's code for a passphrase.
 */
export async function recoverVaultKey(code: string, how: { passkey: true } | { passphrase: string }):
    Promise<{ keyset: VaultKeysetOut; secretKey?: string }> {
  const ks = await getKeyset()
  const seal = sealsOf(ks).find((s): s is PersonRecoverySeal => s.alg === 'recovery')
  if (!ks.exists || !seal) throw new Error('You have no recovery code on file.')
  let scalar: Uint8Array
  try {
    scalar = await openUserKeyRecovery(seal, ks.user_id, parseRecoveryCode(code))
  } catch {
    throw new Error('That recovery code does not open your vault key.')
  }
  const pub = ks.public_jwk as Record<string, unknown>
  if ('passkey' in how) {
    const touch = await assertWithPrf(ks.prf_input)
    if (!touch.prf) throw new PrfUnsupportedError()
    const added = await sealUserKey(scalar, ks.user_id, touch.cred, touch.prf)
    const keep = sealsOf(ks).filter((s) => !(s.alg === 'prf' && s.cred === touch.cred))
    return { keyset: await putKeyset({ public_jwk: pub, seals: [...keep, added] as unknown as Record<string, unknown>[] }) }
  }
  const problem = passphraseProblem(how.passphrase)
  if (problem) throw new Error(problem)
  const sk = randomBytes(16)
  const added = await sealUserKey2skd(scalar, ks.user_id, how.passphrase, sk)
  const keep = sealsOf(ks).filter((s) => s.alg !== '2skd')
  const keyset = await putKeyset({ public_jwk: pub, seals: [...keep, added] as unknown as Record<string, unknown>[] })
  const skCode = secretKeyCode(sk)
  rememberSecretKey(ks.user_id, skCode)
  return { keyset, secretKey: skCode }
}

/** Create the keyset with ONE passkey touch — which also steps up — and hand
 *  it back unlocked, so the flow that needed it goes on without a second
 *  prompt. Throws PrfUnsupportedError when the passkey gives no PRF. */
export async function createVaultKeyWithPasskey(replace = false, allow?: string[]):
    Promise<{ keyset: VaultKeysetOut; me: Unlocked }> {
  const ks = await getKeyset()
  const prfCapable = (ks.passkeys ?? []).filter((p) => p.prf_enabled === true).map((p) => p.credential_id)
  const touch = await assertWithPrf(ks.prf_input, { allow: allow ?? (prfCapable.length ? prfCapable : undefined) })
  if (!touch.prf) throw new PrfUnsupportedError()
  const pair = await generateKeyPair()
  const seal = await sealUserKey(pair.scalar, ks.user_id, touch.cred, touch.prf)
  const keyset = await putKeyset({ public_jwk: pair.publicJwk as unknown as Record<string, unknown>,
                                   seals: [seal as unknown as Record<string, unknown>], replace })
  return { keyset, me: await unlockedFrom(pair.scalar, ks.user_id, pair.publicJwk) }
}

/** Create the keyset sealed by the passkey the person touches (PRF). */
export async function setUpVaultKey(replace = false): Promise<VaultKeysetOut> {
  return (await createVaultKeyWithPasskey(replace)).keyset
}

/** Create the keyset sealed by a passphrase + a new Secret Key, unlocked. The
 *  Secret Key's code is to show ONCE (the Emergency Kit); it is also
 *  remembered in this browser. */
export async function createVaultKeyWithPassphrase(passphrase: string, replace = false):
    Promise<{ keyset: VaultKeysetOut; secretKey: string; me: Unlocked }> {
  const problem = passphraseProblem(passphrase)
  if (problem) throw new Error(problem)
  const ks = await getKeyset()
  const pair = await generateKeyPair()
  const sk = randomBytes(16)
  const seal = await sealUserKey2skd(pair.scalar, ks.user_id, passphrase, sk)
  const keyset = await putKeyset({ public_jwk: pair.publicJwk as unknown as Record<string, unknown>,
                                   seals: [seal as unknown as Record<string, unknown>], replace })
  const code = secretKeyCode(sk)
  rememberSecretKey(ks.user_id, code)
  return { keyset, secretKey: code, me: await unlockedFrom(pair.scalar, ks.user_id, pair.publicJwk) }
}

/** Create the keyset sealed by a passphrase + a new Secret Key. */
export async function setUpVaultKeyWithPassphrase(passphrase: string, replace = false):
    Promise<{ keyset: VaultKeysetOut; secretKey: string }> {
  const { keyset, secretKey } = await createVaultKeyWithPassphrase(passphrase, replace)
  return { keyset, secretKey }
}

/** Seal the vault key for one more passkey: unlock, then touch the new one. */
export async function addPasskeyToVaultKey(prompt: UnlockPrompt): Promise<VaultKeysetOut> {
  const ks = await getKeyset()
  const scalar = await openScalar(ks, prompt)
  const unsealed = (ks.passkeys ?? []).filter((p) => !p.sealed).map((p) => p.credential_id)
  const touch = await assertWithPrf(ks.prf_input, { allow: unsealed })
  if (!touch.prf) throw new PrfUnsupportedError()
  if (prfSeals(ks).some((s) => s.cred === touch.cred)) throw new Error('That passkey already holds your vault key.')
  const added = await sealUserKey(scalar, ks.user_id, touch.cred, touch.prf)
  return putKeyset({ public_jwk: ks.public_jwk as Record<string, unknown>,
                     seals: [...((ks.seals ?? []) as Record<string, unknown>[]), added as unknown as Record<string, unknown>] })
}

/** Add (or replace) the passphrase seal: unlock, then seal under a passphrase
 *  and a NEW Secret Key, shown once. */
export async function addPassphraseToVaultKey(prompt: UnlockPrompt, passphrase: string):
    Promise<{ keyset: VaultKeysetOut; secretKey: string }> {
  const problem = passphraseProblem(passphrase)
  if (problem) throw new Error(problem)
  const ks = await getKeyset()
  const scalar = await openScalar(ks, prompt)
  const sk = randomBytes(16)
  const added = await sealUserKey2skd(scalar, ks.user_id, passphrase, sk)
  const keep = ((ks.seals ?? []) as unknown as AnySeal[]).filter((s) => s.alg !== '2skd')
  const keyset = await putKeyset({ public_jwk: ks.public_jwk as Record<string, unknown>,
                                   seals: [...keep, added] as unknown as Record<string, unknown>[] })
  const code = secretKeyCode(sk)
  rememberSecretKey(ks.user_id, code)
  return { keyset, secretKey: code }
}

// ---- wrapping to targets ------------------------------------------------------------------

/** Who holds a wrap now or should: the public key to wrap to, per target. */
export interface Target {
  kind: 'device' | 'member'
  target: string
  publicJwk: Jwk
  ecdhJkt: string
}

export function targetOf(t: ZkTargetOut | ZkWrapOut): Target | null {
  if (t.kind === 'recovery' || !t.target || !t.public_jwk) return null
  return { kind: t.kind, target: t.target, publicJwk: t.public_jwk as Jwk, ecdhJkt: t.ecdh_jkt ?? '' }
}

async function wrapTo(vk: Uint8Array, agentId: string, kv: number, t: Target): Promise<ZkWrapIn> {
  const info = t.kind === 'device' ? deviceInfo(agentId, kv, t.ecdhJkt) : memberInfo(agentId, kv, t.target)
  const wrap = await wrapEcdhEs(vk, t.publicJwk, info)
  return { kind: t.kind, target: t.target, wrap: wrap as unknown as Record<string, unknown> }
}

/** VK at the current kv, opened with the person's keyset. */
async function openVk(state: ZkStateOut, me: Unlocked): Promise<Uint8Array> {
  const mine = state.wraps.find((w) => w.kind === 'member' && w.kv === state.kv && w.target === me.userId)
  if (!mine) throw new Error('You hold no wrap of this agent\'s vault key — ask a member to grant you, or recover with the code.')
  return unwrapEcdhEs(mine.wrap as unknown as EcdhWrap, me.privateKey, me.publicJwk,
    memberInfo(state.agent_id, state.kv, me.userId))
}

/** Every principal that should read the agent now — what a `zks1` value is
 *  sealed to (a device by its ECDH key's thumbprint, a person by user id). */
function recipientsOf(state: ZkStateOut): Recipient[] {
  const out = new Map<string, Recipient>()
  for (const t of [...state.wraps.filter((w) => w.kv === state.kv && w.current), ...state.pending]) {
    const tg = targetOf(t)
    if (!tg) continue
    const target = tg.kind === 'device' ? tg.ecdhJkt : tg.target
    if (target) out.set(`${tg.kind}:${target}`, { kind: tg.kind, target, publicJwk: tg.publicJwk })
  }
  return [...out.values()]
}

/** A stored value's plaintext: `zk1` under VK, or `zks1` sealed to me. */
async function openStored(state: ZkStateOut, vk: Uint8Array | null, me: Unlocked, name: string,
                          stored: string): Promise<string> {
  if (isSealed(stored)) return openSealedValue(stored, state.agent_id, name, 'member', me.userId, me.privateKey)
  if (!vk) throw new Error(`${name} needs the vault key, which this agent does not have yet.`)
  return decryptValue(vk, state.agent_id, name, state.kv, stored)
}

/**
 * Settle: re-encrypt every value canopy-web sealed to recipients (`zks1` — a
 * Gmail token, a rotated canopy-pat) as an ordinary `zk1` value under VK, so
 * devices approved after it was minted can read it too. Compare-and-swap: a
 * value minted again meanwhile is left for the next settle. Best effort —
 * returns how many it settled; never throws.
 */
export async function settleSealed(slug: string, vk: Uint8Array, me: Unlocked): Promise<number> {
  try {
    const state = await getZkState(slug, true)
    if (state.kv === 0) return 0
    const values = []
    for (const [name, stored] of Object.entries(state.values ?? {})) {
      if (!isSealed(stored)) continue
      try {
        const plain = await openSealedValue(stored, state.agent_id, name, 'member', me.userId, me.privateKey)
        values.push({ name, value_zk: await encryptValue(vk, state.agent_id, name, state.kv, plain),
                      expect: await valueDigest(stored) })
      } catch {
        // sealed before I could read it: a device holding it settles it
      }
    }
    if (!values.length) return 0
    await putZkValues(slug, { kv: state.kv, values })
    return values.length
  } catch {
    // zk_value_changed (minted again meanwhile) or any hiccup: the next touch settles it
    return 0
  }
}

export interface RecoveryOut {
  /** Shown ONCE: the only copy anywhere. */
  code: string
}

async function freshRecovery(vk: Uint8Array, agentId: string, kv: number): Promise<{ wrap: ZkWrapIn; code: string }> {
  const r = randomBytes(32)
  const wrap = await wrapRecovery(vk, agentId, kv, r)
  return { wrap: { kind: 'recovery', target: null, wrap: wrap as unknown as Record<string, unknown> }, code: recoveryCode(r) }
}

// ---- the agent flows ------------------------------------------------------------------------

/**
 * Turn on ZK, step by step: generate VK; fetch the custodial values (reveal,
 * behind the passkey step-up this flow already holds); encrypt them; wrap VK
 * for me, every eligible device and recovery; then hand the recovery code to
 * `confirm` — which shows it once and resolves when the person has kept it —
 * and only then call enable.
 */
export async function turnOnZk(slug: string, storedNames: string[],
                               confirm: (code: string) => Promise<void>, unlock?: Unlock): Promise<ZkStateOut> {
  const ready = await prepareTurnOnZk(slug, storedNames, unlock)
  if (ready.code) await confirm(ready.code)
  return ready.commit()
}

/** Everything turning ZK on needs, done but not yet sent: the recovery code
 *  to show, and `commit` to enable once it has been kept. Several agents can be
 *  prepared, their codes shown together, then committed. */
export interface PreparedZk {
  slug: string
  code: string
  kv: number
  commit: () => Promise<ZkStateOut>
}

export async function prepareTurnOnZk(slug: string, storedNames: string[], unlock?: Unlock,
                                      opts: { personRecovery?: boolean } = {}): Promise<PreparedZk> {
  const ks = await getKeyset()
  if (!ks.exists || !ks.public_jwk) throw new Error('Set up your vault key first (Settings → Vault key).')
  // An agent born zero-knowledge (kv 0) has only sealed values and no vault
  // key yet: this creates it, opening each sealed value with my keyset.
  const state = await getZkState(slug, true)
  if (state.enabled && state.kv !== 0) throw new Error('Zero-knowledge is already on for this agent.')
  const kv = 1
  const vk = randomBytes(32)
  let me: Unlocked | null = null
  const values: { name: string; value_zk: string }[] = []
  for (const name of storedNames) {
    const stored = state.values?.[name]
    let plain: string
    if (stored && isSealed(stored)) {
      if (!unlock) throw new Error('Unlock your vault key to open the sealed values.')
      me = me ?? (await unlock())
      plain = await openSealedValue(stored, state.agent_id, name, 'member', me.userId, me.privateKey)
    } else {
      plain = await revealAgentCredential(slug, name)
    }
    values.push({ name, value_zk: await encryptValue(vk, state.agent_id, name, kv, plain) })
  }
  const wraps: ZkWrapIn[] = [await wrapTo(vk, state.agent_id, kv,
    { kind: 'member', target: ks.user_id, publicJwk: ks.public_jwk as Jwk, ecdhJkt: '' })]
  for (const p of state.pending) {
    const t = targetOf(p)
    if (t && !(t.kind === 'member' && t.target === ks.user_id)) wraps.push(await wrapTo(vk, state.agent_id, kv, t))
  }
  // What I wrap the vault key to, I vouch for (zk-v1 §7) — with the key this
  // flow already opened; giveAccess hands its (memoised) unlock in as well.
  const readers: Endorsable[] = [selfReader(ks.user_id, ks.public_jwk as Jwk),
    ...state.pending.map(targetOf).filter((t): t is Target => t !== null).map(endorsableOf)]
  const commit = async () => {
    const out = await enableZk(slug, { kv, wraps, values })
    const signer = me ?? (opts.personRecovery && unlock ? await unlock().catch(() => null) : null)
    if (signer) await endorseQuietly(slug, state.agent_id, signer, readers)
    return out
  }
  // The person's one recovery code covers this agent (their member wrap opens
  // with it); only someone without one gets a code for this agent alone.
  if (opts.personRecovery || hasRecoveryCode(ks)) {
    return { slug, code: '', kv, commit }
  }
  const recovery = await freshRecovery(vk, state.agent_id, kv)
  wraps.push(recovery.wrap)
  return { slug, code: recovery.code, kv, commit }
}

/**
 * The person's vault key, unlocked, for a flow that needs it — created on the
 * spot when they have none, with as few touches as their passkeys allow:
 * one PRF touch (which also steps up), or a replacement passkey first when
 * theirs predates PRF. `null` when a passphrase setup is the only way left
 * (the caller shows it inline). `replaced` lists passkeys an upgrade made
 * redundant, for the caller to offer deleting.
 */
export async function vaultKeyForFlow(browser: boolean | null, prompt: UnlockPrompt):
    Promise<{ me: Unlocked; replaced: KeysetPasskey[] } | null> {
  const ks = await getKeyset()
  if (ks.exists && ks.public_jwk) {
    return { me: (await stepUpAndUnlock(ks)) ?? (await unlockVaultKey(prompt, ks)), replaced: [] }
  }
  const passkeys = ks.passkeys ?? []
  const route = vaultKeyRoute(passkeys, browser)
  if (route === 'passphrase') return null
  let allow: string[] | undefined
  let replaced: KeysetPasskey[] = []
  if (route === 'upgrade') {
    const old = passkeys.find((p) => p.prf_enabled !== true)
    const fresh = await upgradePasskey(old?.label || defaultLabel())
    if (fresh.prf_enabled !== true) return null
    allow = [fresh.credential_id]
    replaced = passkeys.filter((p) => p.prf_enabled !== true && p.credential_id !== fresh.credential_id)
  }
  try {
    return { me: (await createVaultKeyWithPasskey(false, allow)).me, replaced }
  } catch (e) {
    if (e instanceof PrfUnsupportedError) return null
    throw e
  }
}

export type KeysetPasskey = NonNullable<VaultKeysetOut['passkeys']>[number]

/** Grant: unwrap VK with my keyset, wrap it for every pending target. */
export async function grantPending(slug: string, unlock: Unlock): Promise<ZkStateOut> {
  const state = await getZkState(slug)
  const targets = state.pending.map(targetOf).filter((t): t is Target => t !== null)
  const sealed = state.sealed ?? []
  if (state.kv === 0 || (!targets.length && !sealed.length)) return state
  const me = await unlock()
  const vk = await openVk(state, me)
  let out = state
  if (targets.length) {
    const wraps = await Promise.all(targets.map((t) => wrapTo(vk, state.agent_id, state.kv, t)))
    out = await addZkWraps(slug, { kv: state.kv, wraps })
    await endorseQuietly(slug, state.agent_id, me, [selfReader(me.userId, me.publicJwk), ...targets.map(endorsableOf)])
  }
  // The same unlock settles what canopy-web sealed, so the newly granted can read it.
  if (sealed.length && (await settleSealed(slug, vk, me))) out = await getZkState(slug)
  return out
}

/**
 * Everything an agent needs from my key so every machine and person allowed
 * to read it can: an agent born zero-knowledge (kv 0) gets its vault key now —
 * its sealed values opened with my key and re-encrypted under it, no recovery
 * code of its own (mine covers it) — then whoever is waiting is let in. Runs
 * inside a passkey touch the person is already making (approving a machine),
 * so no agent ever needs setting up by hand.
 */
export async function giveAccess(slug: string, unlockOnce: Unlock): Promise<ZkStateOut> {
  let opened: Promise<Unlocked> | null = null
  const unlock: Unlock = () => (opened = opened ?? unlockOnce())
  const state = await getZkState(slug, true)
  if (state.enabled && state.kv === 0) {
    const names = Object.keys(state.values ?? {})
    const ready = await prepareTurnOnZk(slug, names, unlock, { personRecovery: true })
    await ready.commit()
  }
  return grantPending(slug, unlock)
}

/** Encrypt one value under the current VK (Save on a ZK agent). */
export async function setZkValue(slug: string, name: string, plaintext: string, unlock: Unlock) {
  const state = await getZkState(slug)
  if (state.kv === 0) {
    // No vault key yet (born zero-knowledge): seal it to every recipient here.
    return putZkValues(slug, { kv: 0,
      values: [{ name, value_zk: await sealValue(plaintext, state.agent_id, name, 0, recipientsOf(state)) }] })
  }
  const vk = await openVk(state, await unlock())
  return putZkValues(slug, { kv: state.kv,
    values: [{ name, value_zk: await encryptValue(vk, state.agent_id, name, state.kv, plaintext) }] })
}

/** Decrypt one value in the browser (Reveal on a ZK agent). */
export async function revealZkValue(slug: string, name: string, unlock: Unlock): Promise<string> {
  const state = await getZkState(slug, true)
  const stored = state.values?.[name]
  if (!stored) throw new Error(`No value is stored for ${name}`)
  const me = await unlock()
  if (isSealed(stored)) return openStored(state, null, me, name, stored)
  return decryptValue(await openVk(state, me), state.agent_id, name, state.kv, stored)
}

/**
 * Rotate: a fresh VK at kv+1; every value re-encrypted; a wrap for every
 * holder that still qualifies (a revoked device or removed admin is left out —
 * that is the point) and every pending target; a fresh recovery code.
 */
export async function rotateVaultKey(slug: string, confirm: (code: string) => Promise<void>,
                                     unlock: Unlock): Promise<ZkStateOut> {
  const state = await getZkState(slug, true)
  const me = await unlock()
  const oldVk = await openVk(state, me)
  const kv = state.kv + 1
  const vk = randomBytes(32)
  const values = []
  for (const [name, stored] of Object.entries(state.values ?? {})) {
    const plain = await openStored(state, oldVk, me, name, stored)
    values.push({ name, value_zk: await encryptValue(vk, state.agent_id, name, kv, plain) })
  }
  const seen = new Set<string>()
  const wraps: ZkWrapIn[] = []
  for (const t of [...state.wraps.filter((w) => w.kv === state.kv && w.current), ...state.pending]) {
    const target = targetOf(t)
    if (!target || seen.has(`${target.kind}:${target.target}`)) continue
    seen.add(`${target.kind}:${target.target}`)
    wraps.push(await wrapTo(vk, state.agent_id, kv, target))
  }
  if (!hasRecoveryCode(await getKeyset())) {
    const recovery = await freshRecovery(vk, state.agent_id, kv)
    wraps.push(recovery.wrap)
    await confirm(recovery.code)
  }
  const out = await rotateZk(slug, { new_kv: kv, wraps, values })
  const readers = [...state.wraps.filter((w) => w.kv === state.kv && w.current), ...state.pending]
    .map(targetOf).filter((t): t is Target => t !== null).map(endorsableOf)
  await endorseQuietly(slug, state.agent_id, me, [selfReader(me.userId, me.publicJwk), ...readers])
  return out
}

// ---- endorsements (zk-v1 §7; spec 2026-10-19-give-an-agent-a-secret-design.md §4) ----
//
// Whenever a person wraps the vault key to someone, they also sign "this key may
// read this agent" — so a writer who holds no vault key can check who a sealed
// value is for instead of trusting canopy-web's list (`./trust`).

export interface Endorsable { kind: 'device' | 'member'; target: string; publicJwk: Jwk }

const selfReader = (userId: string, publicJwk: Jwk): Endorsable => ({ kind: 'member', target: userId, publicJwk })

/** A wrap target as §6/§7 name it: a device by its ECDH thumbprint. */
const endorsableOf = (t: Target): Endorsable =>
  ({ kind: t.kind, target: t.kind === 'device' ? t.ecdhJkt : t.target, publicJwk: t.publicJwk })

/** Sign and store; never throws (a grant must not fail on its receipt). */
async function endorseQuietly(slug: string, agentId: string, me: Unlocked, readers: Endorsable[]): Promise<number> {
  try {
    return await endorseReaders(slug, agentId, me, readers)
  } catch {
    return 0
  }
}

/** Vouch, with my own key, that each of `readers` may read the agent. */
export async function endorseReaders(slug: string, agentId: string, me: Unlocked, readers: Endorsable[]): Promise<number> {
  if (!me.scalar) throw new Error('Unlock your vault key again to confirm who can read this agent.')
  const seen = new Set<string>()
  const items = []
  for (const r of readers) {
    if (!r.target || seen.has(`${r.kind}:${r.target}`)) continue
    seen.add(`${r.kind}:${r.target}`)
    items.push(await endorse(me.scalar, me.publicJwk, agentId, r.kind, r.target, r.publicJwk))
  }
  return items.length ? (await addZkEndorsements(slug, items)).stored : 0
}

/**
 * Seal one value in this browser and store it — the seal card's write. Only
 * `zks1` leaves the page, sealed to the recipients `assess` verified. With a
 * request id it answers the agent's request; without, it is an admin's write.
 */
export async function sealAndStore(slug: string, name: string, plaintext: string, a: Assessment,
                                   requestId?: string): Promise<void> {
  if (!a.sealTo.length) throw new Error('Nobody you trust can read this agent yet — nothing was sent.')
  const value_zk = await sealValue(plaintext, a.agentId, name, a.kv, a.sealTo)
  if (requestId) await fulfilSecretRequest(slug, requestId, { kv: a.kv, value_zk })
  else await putZkValues(slug, { kv: a.kv, values: [{ name, value_zk }] })
}

/** Recover: open VK with the recovery code, wrap it to my keyset. */
export async function recoverWithCode(slug: string, code: string): Promise<ZkStateOut> {
  const secret = parseRecoveryCode(code)
  const [state, ks] = await Promise.all([getZkState(slug), getKeyset()])
  if (!ks.exists || !ks.public_jwk) throw new Error('Set up your vault key first (Settings → Vault key).')
  const rec = state.wraps.find((w) => w.kind === 'recovery' && w.kv === state.kv)
  if (!rec) throw new Error('This agent has no recovery wrap at its current vault key.')
  let vk: Uint8Array
  try {
    vk = await unwrapRecovery(rec.wrap as unknown as RecoveryWrap, state.agent_id, state.kv, secret)
  } catch {
    throw new Error('That recovery code does not open this agent\'s vault key (it may be from before a rotation).')
  }
  const wrap = await wrapTo(vk, state.agent_id, state.kv,
    { kind: 'member', target: ks.user_id, publicJwk: ks.public_jwk as Jwk, ecdhJkt: '' })
  return addZkWraps(slug, { kv: state.kv, wraps: [wrap] })
}

/**
 * Approve a device key AND hand it every zero-knowledge vault key it will need
 * (`agents`, from the key's listing) — in ONE passkey touch: the assertion
 * that steps up for the approval also yields the PRF output that opens my
 * keyset. A keyset sealed only by a passphrase steps up as usual and then asks
 * for the passphrase (`prompt`). Returns the approved key and the agents
 * granted; an agent that fails to grant is left for the Grant button.
 */
export async function approveAndGrant(keyId: string, agents: string[], prompt: UnlockPrompt):
    Promise<{ key: RunnerDeviceKeyOut; granted: string[]; failed: string[] }> {
  const ks = agents.length ? await getKeyset() : null
  let me: Unlocked | null = ks ? await stepUpAndUnlock(ks) : null
  const key = await approveRunnerDeviceKey(keyId)
  const granted: string[] = [], failed: string[] = []
  const todo = key.zk_pending_agents ?? agents
  if (!todo.length || !ks?.exists) return { key, granted, failed: [...todo] }
  me = me ?? (await unlockVaultKey(prompt, ks))
  for (const slug of todo) {
    try {
      await giveAccess(slug, async () => me!)
      granted.push(slug)
    } catch {
      failed.push(slug)
      // I could not hand this agent's vault key over myself (I hold no copy of
      // it yet). Still vouch for the machine with my own key: a keeper — a
      // runner that holds the vault key — wraps only to endorsed machines, so
      // this is what lets it pass the key on without anyone being asked
      // (spec 2026-10-22-vault-access-follows-canopy-access-design.md §1).
      await endorseMachine(slug, key, me!)
    }
  }
  return { key, granted, failed }
}

/** Sign "this machine may read this agent" with my own key; best effort. */
async function endorseMachine(slug: string, key: RunnerDeviceKeyOut, me: Unlocked): Promise<void> {
  if (!key.ecdh_jkt || !key.ecdh_public_jwk) return
  try {
    const state = await getZkState(slug)
    await endorseQuietly(slug, state.agent_id, me,
      [{ kind: 'device', target: key.ecdh_jkt, publicJwk: key.ecdh_public_jwk as Jwk }])
  } catch {
    // Nothing more to do here: the agent's page offers "Confirm who can read".
  }
}

export type { PublicJwk }
