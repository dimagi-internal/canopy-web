/** The person's own vault key: open it with a passkey or a passphrase, and remember it for a flow. */
import { registerThisDevice, type PasskeyOut } from '@/api/passkeys'
import { } from '@/auth/webauthn'
import {
  getKeyset,
  type VaultKeysetOut,
} from '@/api/vault'
import { } from '@/api/problem'
import { assertWithPrf, PrfUnsupportedError } from './prf'
import type { } from './trust'
import {
  importPrivateScalar,
  openUserKey,
  openUserKey2skd,
  parseSecretKeyCode,
  secretKeyCode,
  type AnySeal,
  type PassphraseSeal,
  type PublicJwk,
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
// (`prf`), or a passphrase + Secret Key (`2skd`). The Secret
// Key is remembered in this browser (localStorage, per user) so unlocking here
// needs only the passphrase; on a new device the person types both.
//
// Deliberate, and why CodeQL's clear-text-storage alert (#39) is dismissed: the
// Secret Key alone opens nothing — it is one of TWO factors, mixed with the
// passphrase (which is never stored) in 2SKD. Its job is to make a stolen
// copy of canopy-web's database un-guessable, not to resist this browser's own
// storage.

const SK_STORE = (userId: string) => `canopy-vault-secret-key:${userId}`

export function rememberedSecretKey(userId: string): string | null {
  try {
    return window.localStorage.getItem(SK_STORE(userId))
  } catch {
    return null
  }
}

export function rememberSecretKey(userId: string, code: string) {
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

export const prfSeals = (ks: VaultKeysetOut) => ((ks.seals ?? []) as unknown as AnySeal[]).filter((s): s is UserKeySeal => s.alg === 'prf')
const passSeals = (ks: VaultKeysetOut) =>
  ((ks.seals ?? []) as unknown as AnySeal[]).filter((s): s is PassphraseSeal => s.alg === '2skd')

export async function openScalar(ks: VaultKeysetOut, prompt: UnlockPrompt): Promise<Uint8Array> {
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
 *  registration. Many passkey apps give no
 *  PRF, and for those a passphrase is the way — so a page offers the passkey
 *  route only when this is true, and still falls back if the touch says no. */
export function passkeyCanHoldVaultKey(passkeys: readonly { prf_enabled?: boolean | null }[], browser: boolean | null): boolean {
  return browser !== false && passkeys.some((p) => p.prf_enabled === true)
}

/** How a person without a vault key should get one:
 *  - `passkey`: a passkey of theirs reported PRF — one touch makes the key;
 *  - `upgrade`: none did, but a new passkey may (one registered before PRF was
 *    asked for reports `null`, and a password manager then
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

export async function unlockedFrom(scalar: Uint8Array, userId: string, publicJwk: PublicJwk): Promise<Unlocked> {
  const pub = publicJwk as Jwk
  return { userId, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
}

