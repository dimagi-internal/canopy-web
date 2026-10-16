/**
 * WebAuthn JSON <-> browser shapes (canopy-web#1538).
 *
 * The server (py_webauthn) speaks the WebAuthn JSON form: every binary field is
 * base64url. `navigator.credentials.create/get` take and return ArrayBuffers.
 * These pure helpers translate both ways, hand-rolled rather than relying on
 * `PublicKeyCredential.parseCreationOptionsFromJSON` / `toJSON()`, which older
 * Safari and Firefox still lack.
 */

type Json = Record<string, unknown>

export function base64urlToBuffer(value: string): ArrayBuffer {
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/')
  const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
  const bin = atob(padded)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out.buffer
}

export function bufferToBase64url(buf: ArrayBuffer | ArrayBufferView): string {
  const bytes = buf instanceof ArrayBuffer ? new Uint8Array(buf) : new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength)
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (!Array.isArray(list)) return undefined
  return list.map((d: Json) => ({
    type: 'public-key',
    id: base64urlToBuffer(String(d.id)),
    ...(Array.isArray(d.transports) ? { transports: d.transports as AuthenticatorTransport[] } : {}),
  }))
}

/** Server JSON → the `publicKey` argument of navigator.credentials.create(). */
export function toCreationOptions(json: Json): PublicKeyCredentialCreationOptions {
  const user = json.user as Json
  return {
    ...(json as unknown as PublicKeyCredentialCreationOptions),
    challenge: base64urlToBuffer(String(json.challenge)),
    user: { ...(user as unknown as PublicKeyCredentialUserEntity), id: base64urlToBuffer(String(user.id)) },
    excludeCredentials: descriptors(json.excludeCredentials) ?? [],
  }
}

/** Server JSON → the `publicKey` argument of navigator.credentials.get(). */
export function toRequestOptions(json: Json): PublicKeyCredentialRequestOptions {
  return {
    ...(json as unknown as PublicKeyCredentialRequestOptions),
    challenge: base64urlToBuffer(String(json.challenge)),
    allowCredentials: descriptors(json.allowCredentials) ?? [],
  }
}

/** A PublicKeyCredential from create() or get() → the JSON py_webauthn verifies. */
export function credentialToJSON(cred: PublicKeyCredential): Json {
  const r = cred.response as AuthenticatorResponse & Partial<AuthenticatorAttestationResponse & AuthenticatorAssertionResponse>
  const response: Json = { clientDataJSON: bufferToBase64url(r.clientDataJSON) }
  if (r.attestationObject) {
    response.attestationObject = bufferToBase64url(r.attestationObject)
    if (typeof r.getTransports === 'function') response.transports = r.getTransports()
  }
  if (r.authenticatorData) response.authenticatorData = bufferToBase64url(r.authenticatorData)
  if (r.signature) response.signature = bufferToBase64url(r.signature)
  if (r.userHandle) response.userHandle = bufferToBase64url(r.userHandle)
  return {
    id: cred.id,
    rawId: bufferToBase64url(cred.rawId),
    type: cred.type,
    response,
    clientExtensionResults: typeof cred.getClientExtensionResults === 'function' ? cred.getClientExtensionResults() : {},
    ...(cred.authenticatorAttachment ? { authenticatorAttachment: cred.authenticatorAttachment } : {}),
  }
}

/** A name for a passkey registered on this browser, for the person to edit. */
export function defaultLabel(): string {
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent : ''
  if (/iPhone|iPad/.test(ua)) return 'iPhone / iPad'
  if (/Android/.test(ua)) return 'Android phone'
  if (/Mac OS X/.test(ua)) return 'Mac (Touch ID)'
  if (/Windows/.test(ua)) return 'Windows Hello'
  return 'This device'
}

export function webauthnSupported(): boolean {
  return typeof window !== 'undefined' && typeof window.PublicKeyCredential === 'function' && !!navigator.credentials
}
