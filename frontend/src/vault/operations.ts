/**
 * The zero-knowledge vault flows (canopy-web#1550), composed from the crypto
 * module (`./zk`), the passkey PRF ceremony (`./prf`) and the REST calls
 * (`@/api/vault`). Every secret here — the vault key VK, a person's private
 * scalar, a recovery secret, a decrypted value — lives only in local variables
 * of one call and is never sent anywhere; what goes to canopy-web is
 * ciphertext, wraps and seals (and, for "turn off" only, the plaintexts that
 * return to custodial storage).
 */
import { revealAgentCredential } from '@/api/agents'
import {
  addZkWraps,
  disableZk,
  enableZk,
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
import {
  decryptValue,
  deviceInfo,
  encryptValue,
  generateKeyPair,
  importPrivateScalar,
  memberInfo,
  openUserKey,
  openUserKey2skd,
  parseRecoveryCode,
  parseSecretKeyCode,
  passphraseProblem,
  randomBytes,
  recoveryCode,
  sealUserKey,
  sealUserKey2skd,
  secretKeyCode,
  unwrapEcdhEs,
  unwrapRecovery,
  wrapEcdhEs,
  wrapRecovery,
  type AnySeal,
  type EcdhWrap,
  type PassphraseSeal,
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
}

// ---- the person's vault key -----------------------------------------------------------
//
// A keyset holds one or more SEALS of the private key: a passkey's PRF output
// (`prf`), or a passphrase + Secret Key (`2skd`, 1Password-style). The Secret
// Key is remembered in this browser (localStorage, per user) so unlocking here
// needs only the passphrase; on a new device the person types both.

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
    if (!seal) throw new Error('That passkey does not hold your vault key — use one marked "sealed".')
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

/** Open the keyset — passkey or passphrase, as the person chooses. */
export async function unlockVaultKey(prompt: UnlockPrompt, ks?: VaultKeysetOut): Promise<Unlocked> {
  ks = ks ?? (await getKeyset())
  if (!ks.exists || !ks.public_jwk) throw new Error('Set up your vault key first (Settings → Vault key).')
  const scalar = await openScalar(ks, prompt)
  const pub = ks.public_jwk as Jwk
  return { userId: ks.user_id, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub }
}

/** Create the keyset sealed by the passkey the person touches (PRF). */
export async function setUpVaultKey(replace = false): Promise<VaultKeysetOut> {
  const ks = await getKeyset()
  const touch = await assertWithPrf(ks.prf_input)
  if (!touch.prf) throw new PrfUnsupportedError()
  const pair = await generateKeyPair()
  const seal = await sealUserKey(pair.scalar, ks.user_id, touch.cred, touch.prf)
  return putKeyset({ public_jwk: pair.publicJwk as unknown as Record<string, unknown>,
                     seals: [seal as unknown as Record<string, unknown>], replace })
}

/** Create the keyset sealed by a passphrase + a new Secret Key. Returns the
 *  Secret Key's code to show ONCE (the Emergency Kit); it is also remembered
 *  in this browser. */
export async function setUpVaultKeyWithPassphrase(passphrase: string, replace = false):
    Promise<{ keyset: VaultKeysetOut; secretKey: string }> {
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
  return { keyset, secretKey: code }
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
                               confirm: (code: string) => Promise<void>): Promise<ZkStateOut> {
  const ks = await getKeyset()
  if (!ks.exists || !ks.public_jwk) throw new Error('Set up your vault key first (Settings → Vault key).')
  const state = await getZkState(slug)
  if (state.enabled) throw new Error('Zero-knowledge is already on for this agent.')
  const kv = 1
  const vk = randomBytes(32)
  const values = []
  for (const name of storedNames) {
    const plain = await revealAgentCredential(slug, name)
    values.push({ name, value_zk: await encryptValue(vk, state.agent_id, name, kv, plain) })
  }
  const wraps: ZkWrapIn[] = [await wrapTo(vk, state.agent_id, kv,
    { kind: 'member', target: ks.user_id, publicJwk: ks.public_jwk as Jwk, ecdhJkt: '' })]
  for (const p of state.pending) {
    const t = targetOf(p)
    if (t && !(t.kind === 'member' && t.target === ks.user_id)) wraps.push(await wrapTo(vk, state.agent_id, kv, t))
  }
  const recovery = await freshRecovery(vk, state.agent_id, kv)
  wraps.push(recovery.wrap)
  await confirm(recovery.code)
  return enableZk(slug, { kv, wraps, values })
}

/** Grant: unwrap VK with my keyset, wrap it for every pending target. */
export async function grantPending(slug: string, unlock: Unlock): Promise<ZkStateOut> {
  const state = await getZkState(slug)
  const targets = state.pending.map(targetOf).filter((t): t is Target => t !== null)
  if (!targets.length) return state
  const vk = await openVk(state, await unlock())
  const wraps = await Promise.all(targets.map((t) => wrapTo(vk, state.agent_id, state.kv, t)))
  return addZkWraps(slug, { kv: state.kv, wraps })
}

/** Encrypt one value under the current VK (Save on a ZK agent). */
export async function setZkValue(slug: string, name: string, plaintext: string, unlock: Unlock) {
  const state = await getZkState(slug)
  const vk = await openVk(state, await unlock())
  return putZkValues(slug, { kv: state.kv,
    values: [{ name, value_zk: await encryptValue(vk, state.agent_id, name, state.kv, plaintext) }] })
}

/** Decrypt one value in the browser (Reveal on a ZK agent). */
export async function revealZkValue(slug: string, name: string, unlock: Unlock): Promise<string> {
  const state = await getZkState(slug, true)
  const stored = state.values?.[name]
  if (!stored) throw new Error(`No value is stored for ${name}`)
  return decryptValue(await openVk(state, await unlock()), state.agent_id, name, state.kv, stored)
}

/**
 * Rotate: a fresh VK at kv+1; every value re-encrypted; a wrap for every
 * holder that still qualifies (a revoked device or removed admin is left out —
 * that is the point) and every pending target; a fresh recovery code.
 */
export async function rotateVaultKey(slug: string, confirm: (code: string) => Promise<void>,
                                     unlock: Unlock): Promise<ZkStateOut> {
  const state = await getZkState(slug, true)
  const oldVk = await openVk(state, await unlock())
  const kv = state.kv + 1
  const vk = randomBytes(32)
  const values = []
  for (const [name, stored] of Object.entries(state.values ?? {})) {
    const plain = await decryptValue(oldVk, state.agent_id, name, state.kv, stored)
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
  const recovery = await freshRecovery(vk, state.agent_id, kv)
  wraps.push(recovery.wrap)
  await confirm(recovery.code)
  return rotateZk(slug, { new_kv: kv, wraps, values })
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

/** Turn off: decrypt every value here and hand the plaintexts back for
 *  custodial storage. */
export async function turnOffZk(slug: string, unlock: Unlock): Promise<ZkStateOut> {
  const state = await getZkState(slug, true)
  const vk = await openVk(state, await unlock())
  const values = []
  for (const [name, stored] of Object.entries(state.values ?? {})) {
    values.push({ name, plaintext: await decryptValue(vk, state.agent_id, name, state.kv, stored) })
  }
  return disableZk(slug, { values })
}

export type { PublicJwk }
