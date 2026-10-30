/** An agent's vault: wrap its key to people and machines, turn it on, grant, reveal, rotate, endorse, approve a machine. */
import { } from '@/auth/webauthn'
import {
  addZkWraps,
  enableZk,
  getKeyset,
  getZkState,
  putAgentZkPins,
  putZkValues,
  type ZkStateOut,
  type ZkTargetOut,
  type ZkWrapIn,
  type ZkWrapOut,
} from '@/api/vault'
import { } from '@/api/problem'
import type { } from './trust'
import {
  decryptValue,
  deviceInfo,
  encryptValue,
  isSealed,
  openSealedValue,
  valueDigest,
  type Recipient,
  memberInfo,
  randomBytes,
  recoveryCode,
  unwrapEcdhEs,
  wrapEcdhEs,
  wrapRecovery,
  type EcdhWrap,
} from './zk'

import { type Unlock, type Unlocked } from './userKey'
import { hasRecoveryCode } from './userKeySetup'

import { type Endorsable, endorsableOf, selfReader } from './endorsements'
import { recordGranted } from './agentKeyOps'

import { endorseQuietly } from './endorsements'

type Jwk = { x?: string; y?: string }

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

export async function wrapTo(vk: Uint8Array, agentId: string, kv: number, t: Target): Promise<ZkWrapIn> {
  const info = t.kind === 'device' ? deviceInfo(agentId, kv, t.ecdhJkt) : memberInfo(agentId, kv, t.target)
  const wrap = await wrapEcdhEs(vk, t.publicJwk, info)
  return { kind: t.kind, target: t.target, wrap: wrap as unknown as Record<string, unknown> }
}

/** VK at the current kv, opened with the person's keyset. */
export async function openVk(state: ZkStateOut, me: Unlocked): Promise<Uint8Array> {
  const mine = state.wraps.find((w) => w.kind === 'member' && w.kv === state.kv && w.target === me.userId)
  if (!mine) throw new Error('You hold no wrap of this agent\'s vault key — ask a member to grant you, or recover with the code.')
  return unwrapEcdhEs(mine.wrap as unknown as EcdhWrap, me.privateKey, me.publicJwk,
    memberInfo(state.agent_id, state.kv, me.userId))
}

/** Every principal that should read the agent now — what a `zks1` value is
 *  sealed to (a device by its ECDH key's thumbprint, a person by user id). */
export function recipientsOf(state: ZkStateOut): Recipient[] {
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
export async function openStored(state: ZkStateOut, vk: Uint8Array | null, me: Unlocked, name: string,
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

export async function freshRecovery(vk: Uint8Array, agentId: string, kv: number): Promise<{ wrap: ZkWrapIn; code: string }> {
  const r = randomBytes(32)
  const wrap = await wrapRecovery(vk, agentId, kv, r)
  return { wrap: { kind: 'recovery', target: null, wrap: wrap as unknown as Record<string, unknown> }, code: recoveryCode(r) }
}

// ---- the agent flows ------------------------------------------------------------------------

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
  if (state.kv !== 0) throw new Error('Zero-knowledge is already on for this agent.')
  const kv = 1
  const vk = randomBytes(32)
  let me: Unlocked | null = null
  const values: { name: string; value_zk: string }[] = []
  for (const name of storedNames) {
    const stored = state.values?.[name]
    if (!stored || !isSealed(stored)) throw new Error(`${name} is not sealed to you, so it cannot be moved under a new vault key.`)
    if (!unlock) throw new Error('Unlock your vault key to open the sealed values.')
    me = me ?? (await unlock())
    const plain = await openSealedValue(stored, state.agent_id, name, 'member', me.userId, me.privateKey)
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
    await recordGranted(state, vk, state.pending, (b) => putAgentZkPins(slug, b), () => getZkState(slug))
  }
  // The same unlock settles what canopy-web sealed, so the newly granted can read it.
  if (sealed.length && (await settleSealed(slug, vk, me))) out = await getZkState(slug)
  return out
}

