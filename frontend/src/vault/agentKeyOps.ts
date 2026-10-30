/** After a grant, give access, set or reveal a value, rotate the key — and the keepers' record of who was granted. */
import { } from '@/auth/webauthn'
import {
  getKeyset,
  getZkState,
  putZkValues,
  rotateZk,
  type ZkStateOut,
  type ZkTargetOut,
  type ZkWrapIn,
} from '@/api/vault'
import { ApiError } from '@/api/problem'
import type { } from './trust'
import {
  decryptValue,
  encryptValue,
  isSealed,
  sealValue,
  jkt,
  randomBytes,
} from './zk'

import { type Unlock, type Unlocked } from './userKey'
import { hasRecoveryCode } from './userKeySetup'

import { type Target, freshRecovery, grantPending, openStored, openVk, prepareTurnOnZk, recipientsOf, targetOf, wrapTo } from './agentVault'
import { endorsableOf, selfReader } from './endorsements'

import { endorseQuietly } from './endorsements'

type Jwk = { x?: string; y?: string }

// ---- the keepers' record (canopy-web Vault.keeper_pins_zk) -------------------------------
//
// Which key each person was given a vault's key on, kept in the vault under its own key
// (name `~keeper-pins`), so every keeper refuses a key that changed. An owner or admin
// granting someone here is what confirms their key — a first one, or one they started
// over with — so the grant records it, and keepers trust it from then on.

const PINS_NAME = '~keeper-pins'

type PinsBody = { kv: number; expected_version: number; value: string }

/** Record each PERSON just granted to, with their key's thumbprint. Best effort: the
 *  grant stands either way; a record that does not open is left alone. */
export async function recordGranted(state: ZkStateOut, vk: Uint8Array, granted: readonly ZkTargetOut[],
                                    put: (body: PinsBody) => Promise<unknown>,
                                    reread: () => Promise<ZkStateOut>): Promise<boolean> {
  const people = granted.filter((t) => t.kind === 'member' && t.target && t.public_jwk)
  if (!people.length) return false
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const s = attempt ? await reread() : state
      const rec = s.pins
      let current: Record<string, unknown> = {}
      if (rec?.value && rec.kv === s.kv) {
        const parsed = JSON.parse(await decryptValue(vk, s.agent_id, PINS_NAME, s.kv, rec.value)) as
          { people?: Record<string, unknown> }
        current = { ...(parsed.people ?? {}) }
      }
      const now = new Date().toISOString()
      for (const t of people) {
        current[t.target!] = { jkt: await jkt(t.public_jwk as Jwk), email: t.email ?? '', pinned_at: now }
      }
      const value = await encryptValue(vk, s.agent_id, PINS_NAME, s.kv, JSON.stringify({ v: 1, people: current }))
      try {
        await put({ kv: s.kv, expected_version: rec?.version ?? 0, value })
        return true
      } catch (e) {
        if (!(e instanceof ApiError && e.code === 'keeper_pins_stale')) return false
      }
    }
  } catch {
    // The record did not open (or the write failed): keepers report it; the grant stands.
  }
  return false
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
  if (state.kv === 0) {
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
  const out = await rotateWith(state, me, confirm, (body) => rotateZk(slug, body))
  const readers = [...state.wraps.filter((w) => w.kv === state.kv && w.current), ...state.pending]
    .map(targetOf).filter((t): t is Target => t !== null).map(endorsableOf)
  await endorseQuietly(slug, state.agent_id, me, [selfReader(me.userId, me.publicJwk), ...readers])
  return out
}

/** The rotation itself, for an agent's vault or a workspace vault (the same shapes). */
export async function rotateWith(state: ZkStateOut, me: Unlocked, confirm: (code: string) => Promise<void>,
                          send: (body: { new_kv: number; wraps: ZkWrapIn[]; values: { name: string; value_zk: string }[] })
                            => Promise<ZkStateOut>): Promise<ZkStateOut> {
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
  return send({ new_kv: kv, wraps, values })
}

