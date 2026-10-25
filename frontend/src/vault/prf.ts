/**
 * Passkey + WebAuthn PRF (canopy-web#1550): the key that seals a person's vault
 * key comes from their passkey's PRF output, which never leaves this browser.
 *
 * One assertion does two jobs: it is the ordinary passkey step-up (the server
 * verifies it and opens its five-minute window), and — through
 * `extensions.prf.eval.first = prf_input` — it yields the 32-byte PRF output.
 * The PRF RESULT is stripped from the credential before it is sent to the
 * server: the server needs the signature, never the secret.
 */
import { apiV2 } from '@/api/client.v2'
import { problemMessage } from '@/api/problem'
import { credentialToJSON, toRequestOptions } from '@/auth/webauthn'
import { unb64u } from './zk'

/** The passkey used, or this browser, gave no PRF output. */
export class PrfUnsupportedError extends Error {
  constructor() {
    super(PRF_UNSUPPORTED)
  }
}

/** A passkey request that ended without a passkey: the person dismissed the
 *  browser's sheet, it timed out, or the browser refused to show it (no recent
 *  click). WebAuthn calls all three `NotAllowedError`, with a message that
 *  explains none of them. */
export function isPasskeyRefusal(e: unknown): boolean {
  return e instanceof Error && e.name === 'NotAllowedError'
}

export const PASSKEY_NOT_COMPLETED =
  'The passkey check did not finish — it was dismissed, timed out, or the browser blocked it — so nothing ' +
  'was changed. Click again to retry.'

export const PRF_UNSUPPORTED =
  'This passkey or browser does not support the WebAuthn PRF extension, which the vault key needs. ' +
  'Use a passkey that does — iCloud Keychain on Safari 18+ / macOS 15+, Google Password Manager or ' +
  'Windows Hello on a current Chrome or Edge, or a security key with hmac-secret on Chrome or Firefox — ' +
  'and try again.'

/** What a person is told when their passkey can't hold the vault key — the
 *  common case with a password-manager passkey, so plain words, not the
 *  browser-support list above. */
export const USE_A_PASSPHRASE =
  "Your passkey app can't unlock a vault key, so use a passphrase — save it and the Secret Key in your password manager."

/** Best-effort early hint (`PublicKeyCredential.getClientCapabilities`, where
 *  the browser has it). `null` = cannot tell until a passkey is used. */
export async function browserSupportsPrf(): Promise<boolean | null> {
  const pkc = (globalThis as { PublicKeyCredential?: { getClientCapabilities?: () => Promise<Record<string, boolean>> } })
    .PublicKeyCredential
  if (!pkc) return false
  if (typeof pkc.getClientCapabilities !== 'function') return null
  try {
    const caps = await pkc.getClientCapabilities()
    return typeof caps['extension:prf'] === 'boolean' ? caps['extension:prf'] : null
  } catch {
    return null
  }
}

export interface PrfAssertion {
  /** The credential id (base64url) of the passkey the person used. */
  cred: string
  /** Its PRF output for `prf_input`, or null when it gave none. */
  prf: Uint8Array | null
}

function stripPrfResults(json: Record<string, unknown>): Record<string, unknown> {
  const ext = { ...((json.clientExtensionResults as Record<string, unknown>) ?? {}) }
  delete ext.prf
  return { ...json, clientExtensionResults: ext }
}

/** Assert a passkey with PRF, and step up with the same touch. */
export async function assertWithPrf(prfInputB64u: string, opts: { allow?: string[] } = {}): Promise<PrfAssertion> {
  const begin = await apiV2.POST('/api/passkeys/step-up/begin')
  if (!begin.data) {
    throw new Error(problemMessage((begin as { error?: unknown }).error, 'Could not start the passkey check'))
  }
  const options = toRequestOptions(begin.data.options as Record<string, unknown>)
  if (opts.allow) {
    options.allowCredentials = (options.allowCredentials ?? []).filter((d) =>
      opts.allow!.includes(bufferId(d.id as ArrayBuffer)))
  }
  const publicKey: PublicKeyCredentialRequestOptions = {
    ...options,
    extensions: { ...(options.extensions ?? {}), prf: { eval: { first: unb64u(prfInputB64u) } } } as AuthenticationExtensionsClientInputs,
  }
  const asserted = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null
  if (!asserted) throw new Error('No passkey was used')
  const ext = asserted.getClientExtensionResults() as { prf?: { results?: { first?: BufferSource } } }
  const first = ext.prf?.results?.first
  const prf = !first ? null
    : first instanceof ArrayBuffer ? new Uint8Array(first).slice()
      : new Uint8Array((first as ArrayBufferView).buffer, (first as ArrayBufferView).byteOffset,
        (first as ArrayBufferView).byteLength).slice()
  const finish = await apiV2.POST('/api/passkeys/step-up/finish', {
    body: { credential: stripPrfResults(credentialToJSON(asserted)) },
  })
  if (!finish.data) {
    throw new Error(problemMessage((finish as { error?: unknown }).error, 'The passkey could not be verified'))
  }
  return { cred: asserted.id, prf }
}

function bufferId(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
