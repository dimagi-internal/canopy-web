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
import { approveRunnerDeviceKey, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
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
  isSealed,
  openSealedValue,
  sealValue,
  valueDigest,
  type Recipient,
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
  return { userId: ks.user_id, privateKey: await importPrivateScalar(await openUserKey(seal, ks.user_id, touch.prf), pub),
           publicJwk: pub }
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
  const ks = await getKeyset()
  if (!ks.exists || !ks.public_jwk) throw new Error('Set up your vault key first (Settings → Vault key).')
  // An agent born zero-knowledge (kv 0) has only sealed values and no vault
  // key yet: this creates it, opening each sealed value with my keyset.
  const state = await getZkState(slug, true)
  if (state.enabled && state.kv !== 0) throw new Error('Zero-knowledge is already on for this agent.')
  const kv = 1
  const vk = randomBytes(32)
  let me: Unlocked | null = null
  const values = []
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
  const recovery = await freshRecovery(vk, state.agent_id, kv)
  wraps.push(recovery.wrap)
  await confirm(recovery.code)
  return enableZk(slug, { kv, wraps, values })
}

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
  }
  // The same unlock settles what canopy-web sealed, so the newly granted can read it.
  if (sealed.length && (await settleSealed(slug, vk, me))) out = await getZkState(slug)
  return out
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
  const me = await unlock()
  const vk = state.kv === 0 ? null : await openVk(state, me)
  const values = []
  for (const [name, stored] of Object.entries(state.values ?? {})) {
    values.push({ name, plaintext: await openStored(state, vk, me, name, stored) })
  }
  return disableZk(slug, { values })
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
      await grantPending(slug, async () => me!)
      granted.push(slug)
    } catch {
      failed.push(slug)
    }
  }
  return { key, granted, failed }
}

export type { PublicJwk }
