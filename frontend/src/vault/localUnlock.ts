/**
 * Unlock a vault key from a keyset already in hand, with no request to canopy-web —
 * for the embedded panel, which reaches canopy through a site's token rather than a
 * browser session, so it cannot read `/api/vault/keyset` or step up a passkey there.
 * The keyset (public half and seals, never a private key) comes with the
 * conversation (`sealed_key.keyset`).
 *
 * A passkey here is a plain WebAuthn assertion asking for its PRF output; nothing
 * is verified server-side because nothing is authorised — the PRF output only opens
 * the seal in this browser. A frame the browser will not let use passkeys falls back
 * to the passphrase, and the panel offers to read the conversation in canopy itself.
 */
import { rememberSelf } from './trust'
import type { Unlocked, UnlockChoice, UnlockPrompt } from './operations'
import { rememberedSecretKey } from './operations'
import { PrfUnsupportedError } from './prf'
import {
  importPrivateScalar,
  openUserKey,
  openUserKey2skd,
  parseSecretKeyCode,
  randomBytes,
  unb64u,
  type AnySeal,
  type PassphraseSeal,
  type UserKeySeal,
} from './zk'

export interface KeysetInHand {
  user_id: string
  prf_input: string
  public_jwk: object
  seals: object[]
}

type Jwk = { x?: string; y?: string }

async function prfLocally(ks: KeysetInHand, seals: UserKeySeal[]): Promise<{ cred: string; prf: Uint8Array }> {
  const asserted = (await navigator.credentials.get({
    publicKey: {
      challenge: randomBytes(32),
      allowCredentials: seals.map((s) => ({ type: 'public-key' as const, id: unb64u(s.cred) })),
      userVerification: 'preferred',
      extensions: { prf: { eval: { first: unb64u(ks.prf_input) } } } as AuthenticationExtensionsClientInputs,
    },
  })) as PublicKeyCredential | null
  if (!asserted) throw new Error('No passkey was used')
  const first = (asserted.getClientExtensionResults() as { prf?: { results?: { first?: BufferSource } } })
    .prf?.results?.first
  if (!first) throw new PrfUnsupportedError()
  const prf = first instanceof ArrayBuffer ? new Uint8Array(first).slice()
    : new Uint8Array((first as ArrayBufferView).buffer, (first as ArrayBufferView).byteOffset,
      (first as ArrayBufferView).byteLength).slice()
  return { cred: asserted.id, prf }
}

/** Open `ks` with ONE click: a passkey straight away when the keyset has one — that
 *  click is the user gesture WebAuthn needs, so nothing is awaited before it — and
 *  `prompt` (a passphrase) only when there is no passkey seal, or the passkey is
 *  unavailable here or failed. */
export async function unlockLocally(ks: KeysetInHand, prompt: UnlockPrompt): Promise<Unlocked> {
  const seals = (ks.seals ?? []) as AnySeal[]
  const byPasskey = seals.filter((s): s is UserKeySeal => s.alg === 'prf')
  const byPass = seals.filter((s): s is PassphraseSeal => s.alg === '2skd')
  if (!byPasskey.length && !byPass.length) throw new Error('Your vault key has no way to unlock it here.')
  const pub = ks.public_jwk as Jwk
  const done = async (scalar: Uint8Array): Promise<Unlocked> => {
    // Pinned as this person's own key, so a key the server later names as theirs is
    // checked against it (`trust.checkSelf`).
    await rememberSelf(ks.user_id, pub, scalar).catch(() => undefined)
    return { userId: ks.user_id, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
  }
  if (byPasskey.length) {
    try {
      const touch = await prfLocally(ks, byPasskey)
      const seal = byPasskey.find((s) => s.cred === touch.cred)
      if (!seal) throw new Error('That passkey does not hold your vault key.')
      return await done(await openUserKey(seal, ks.user_id, touch.prf))
    } catch (e) {
      if (!byPass.length) throw e
      // The passkey could not open it here: the passphrase is the second step.
    }
  }
  const choice: UnlockChoice = await prompt({
    passkey: false, passphrase: true, needsSecretKey: !rememberedSecretKey(ks.user_id),
  })
  if (choice.method !== 'passphrase') throw new Error('Unlock cancelled — nothing was changed.')
  const code = choice.secretKey?.trim() || rememberedSecretKey(ks.user_id)
  if (!code) throw new Error('Enter your Secret Key (it starts with CV1-, on your Emergency Kit).')
  const sk = parseSecretKeyCode(code)
  for (const seal of byPass) {
    try {
      return await done(await openUserKey2skd(seal, ks.user_id, choice.passphrase, sk))
    } catch {
      // the next passphrase seal, if any
    }
  }
  throw new Error('That passphrase and Secret Key do not open your vault key.')
}
