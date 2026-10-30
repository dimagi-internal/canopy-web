/** A workspace vault (and a runner's own): make its key, grant, rotate, seal a value. */
import { } from '@/auth/webauthn'
import {
  addWorkspaceVaultWraps,
  enableWorkspaceVaultZk,
  getKeyset,
  getWorkspaceVaultZk,
  putWorkspaceVaultPins,
  putWorkspaceVaultValues,
  rotateWorkspaceVaultZk,
  type ZkStateOut,
  type ZkTargetOut,
  type ZkWrapIn,
} from '@/api/vault'
import { } from '@/api/problem'
import type { } from './trust'
import {
  encryptValue,
  randomBytes,
} from './zk'

import { type Unlocked } from './userKey'
import { hasRecoveryCode } from './userKeySetup'
import { type Target, openVk, targetOf, wrapTo } from './agentVault'
import { recordGranted, rotateWith } from './agentKeyOps'


// ---- a workspace vault: a runner's own (spec 2026-10-23-runner-claude-credentials-in-the-vault) ----
// Its zero-knowledge routes take the agent vault's shapes with the vault's binding id as
// `agent_id`, so these are the agent flows above, pointed at `/api/vaults/{id}/zk`.

/**
 * Make a workspace vault's first key (kv 1), with a copy for me and for everyone and
 * every machine already waiting (`pending` — for a runner's vault, the box itself). My
 * own recovery code covers it (my copy opens with it), so I need one first; canopy-web
 * never sees the key.
 */
export async function makeWorkspaceVaultKey(vaultId: string, me: Unlocked): Promise<ZkStateOut> {
  const ks = await getKeyset()
  if (!hasRecoveryCode(ks)) {
    throw new Error('Add a recovery code to your vault key first (Settings → Vault key): it is how this vault comes back if you lose your passkey.')
  }
  const state = await getWorkspaceVaultZk(vaultId)
  if (state.kv !== 0) return state
  const kv = 1
  const vk = randomBytes(32)
  const wraps: ZkWrapIn[] = [await wrapTo(vk, state.agent_id, kv,
    { kind: 'member', target: me.userId, publicJwk: me.publicJwk, ecdhJkt: '' })]
  for (const p of state.pending) {
    const t = targetOf(p)
    if (t && !(t.kind === 'member' && t.target === me.userId)) wraps.push(await wrapTo(vk, state.agent_id, kv, t))
  }
  return enableWorkspaceVaultZk(vaultId, { kv, wraps, values: [] })
}

/** Give everyone and every machine waiting a copy of the vault's key, opened with mine. */
export async function grantWorkspaceVaultPending(
  vaultId: string, me: Unlocked, only?: (t: ZkTargetOut) => boolean,
): Promise<ZkStateOut> {
  const state = await getWorkspaceVaultZk(vaultId)
  // `only` narrows the grant to some of those waiting (one runner's row).
  const pending = only ? state.pending.filter(only) : state.pending
  const targets = pending.map(targetOf).filter((t): t is Target => t !== null)
  if (state.kv === 0 || !targets.length) return state
  const vk = await openVk(state, me)
  const wraps = await Promise.all(targets.map((t) => wrapTo(vk, state.agent_id, state.kv, t)))
  const out = await addWorkspaceVaultWraps(vaultId, { kv: state.kv, wraps })
  // This grant is what confirms each person's key (a first one, or a new one): record it.
  await recordGranted(state, vk, pending, (b) => putWorkspaceVaultPins(vaultId, b),
    () => getWorkspaceVaultZk(vaultId))
  return out
}

/**
 * Rotate a workspace vault's key, as an agent's vault rotates: a fresh key, every value
 * re-encrypted, a copy for every member and machine that still qualifies (one removed
 * since is left out — that is the point) and everyone waiting. My own recovery code is
 * the way back, as when the key was made, so I need one first.
 */
export async function rotateWorkspaceVaultKey(vaultId: string, me: Unlocked): Promise<ZkStateOut> {
  if (!hasRecoveryCode(await getKeyset())) {
    throw new Error('Add a recovery code to your vault key first (Settings → Vault key): it is how this vault comes back if you lose your passkey.')
  }
  const state = await getWorkspaceVaultZk(vaultId, true)
  if (state.kv === 0) throw new Error('This vault has no key yet: make its key first.')
  return rotateWith(state, me, async () => { throw new Error('A recovery code is needed first.') },
    (body) => rotateWorkspaceVaultZk(vaultId, body))
}

/** Seal one value into a workspace vault, in this browser: canopy-web stores ciphertext only. */
export async function sealWorkspaceVaultValue(vaultId: string, name: string, plaintext: string, me: Unlocked) {
  const state = await getWorkspaceVaultZk(vaultId)
  if (state.kv === 0) throw new Error('This vault has no key yet: make its key first.')
  const vk = await openVk(state, me)
  return putWorkspaceVaultValues(vaultId, { kv: state.kv,
    values: [{ name, value_zk: await encryptValue(vk, state.agent_id, name, state.kv, plaintext) }] })
}


