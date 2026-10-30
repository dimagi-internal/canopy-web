/** Setting up the person's vault key (passkey or passphrase), adding a seal, and the ONE recovery code. */
import { defaultLabel } from '@/auth/webauthn'
import {
  getKeyset,
  putKeyset,
  type VaultKeysetOut,
} from '@/api/vault'
import { } from '@/api/problem'
import { assertWithPrf, PrfUnsupportedError } from './prf'
import type { } from './trust'
import {
  generateKeyPair,
  openUserKeyRecovery,
  parseRecoveryCode,
  passphraseProblem,
  randomBytes,
  recoveryCode,
  sealUserKey,
  sealUserKey2skd,
  sealUserKeyRecovery,
  secretKeyCode,
  type AnySeal,
  type PersonRecoverySeal,
} from './zk'

import { type UnlockPrompt, openScalar, prfSeals, rememberSecretKey, stepUpAndUnlock, unlockVaultKey, unlockedFrom, upgradePasskey, vaultKeyRoute } from './userKey'
import { type Unlocked } from './userKey'



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

