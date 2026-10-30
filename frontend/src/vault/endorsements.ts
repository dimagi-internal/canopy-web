/** Endorsements (zk-v1 §7), sealing a value to endorsed readers, recovery with a code, and approving or vouching for a machine. */
import { approveRunnerDeviceKey, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { } from '@/auth/webauthn'
import {
  addZkEndorsements,
  addZkWraps,
  fulfilSecretRequest,
  getKeyset,
  getZkState,
  putZkValues,
  type ZkStateOut,
} from '@/api/vault'
import { } from '@/api/problem'
import type { Assessment } from './trust'
import {
  endorse,
  sealValue,
  parseRecoveryCode,
  unwrapRecovery,
  type RecoveryWrap,
} from './zk'

import { type UnlockPrompt, type Unlocked, stepUpAndUnlock, unlockVaultKey } from './userKey'

import { type Target, wrapTo } from './agentVault'
import { giveAccess } from './agentKeyOps'

type Jwk = { x?: string; y?: string }

// ---- endorsements (zk-v1 §7; spec 2026-10-19-give-an-agent-a-secret-design.md §4) ----
//
// Whenever a person wraps the vault key to someone, they also sign "this key may
// read this agent" — so a writer who holds no vault key can check who a sealed
// value is for instead of trusting canopy-web's list (`./trust`).

export interface Endorsable { kind: 'device' | 'member'; target: string; publicJwk: Jwk }

export const selfReader = (userId: string, publicJwk: Jwk): Endorsable => ({ kind: 'member', target: userId, publicJwk })

/** A wrap target as §6/§7 name it: a device by its ECDH thumbprint. */
export const endorsableOf = (t: Target): Endorsable =>
  ({ kind: t.kind, target: t.kind === 'device' ? t.ecdhJkt : t.target, publicJwk: t.publicJwk })

/** Sign and store; never throws (a grant must not fail on its receipt). */
export async function endorseQuietly(slug: string, agentId: string, me: Unlocked, readers: Endorsable[]): Promise<number> {
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
export async function approveAndGrant(keyId: string, agents: string[], prompt: UnlockPrompt, vouch: readonly VouchAgent[] = []):
    Promise<{ key: RunnerDeviceKeyOut; granted: string[]; failed: string[]; vouched: string[] }> {
  const ks = agents.length || vouch.length ? await getKeyset() : null
  let me: Unlocked | null = ks ? await stepUpAndUnlock(ks) : null
  const key = await approveRunnerDeviceKey(keyId)
  const granted: string[] = [], failed: string[] = []
  const todo = key.zk_pending_agents ?? agents
  const toVouch = key.zk_vouch_agents ?? vouch
  if ((!todo.length && !toVouch.length) || !ks?.exists) return { key, granted, failed: [...todo], vouched: [] }
  me = me ?? (await unlockVaultKey(prompt, ks))
  // My own machine, for agents whose vault I am a member of but do not manage: I
  // cannot hand the key over, but my signature is what lets a keeper do it.
  const vouched = await vouchForMyMachine(key, toVouch, me)
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
  return { key, granted, failed, vouched }
}

export type VouchAgent = { slug: string; agent_id: string }

/**
 * Sign "this machine — mine — may read this agent" for each agent whose vault I am a
 * member of without managing it (`zk_vouch_agents`). A keeper (a box that holds the
 * vault key and works for a manager) then passes the key on with nobody asked
 * (spec 2026-10-22-vault-access-follows-canopy-access-design.md §1). Returns the
 * slugs it signed for.
 */
export async function vouchForMyMachine(key: RunnerDeviceKeyOut, agents: readonly VouchAgent[], me: Unlocked): Promise<string[]> {
  if (!key.ecdh_jkt || !key.ecdh_public_jwk) return []
  const done: string[] = []
  for (const a of agents) {
    const n = await endorseQuietly(a.slug, a.agent_id, me,
      [{ kind: 'device', target: key.ecdh_jkt, publicJwk: key.ecdh_public_jwk as Jwk }])
    if (n) done.push(a.slug)
  }
  return done
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


