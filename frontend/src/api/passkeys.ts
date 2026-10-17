/**
 * A person's passkeys and the step-up they earn (`/api/passkeys/`, canopy-web#1538).
 *
 * `registerThisDevice` and `performStepUp` run the whole two-call ceremony,
 * including the browser prompt; everything else is a plain call.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { problemMessage } from './problem'
import { credentialToJSON, toCreationOptions, toRequestOptions } from '../auth/webauthn'

type Schemas = components['schemas']
export type PasskeyOut = Schemas['PasskeyOut']
export type StepUpStatusOut = Schemas['StepUpStatusOut']

function need<T>(res: { data?: T; error?: unknown }, fallback: string): T {
  if (res.error !== undefined || res.data === undefined) throw new Error(problemMessage(res.error, fallback))
  return res.data
}

export async function listPasskeys(): Promise<PasskeyOut[]> {
  return Array.from(need(await apiV2.GET('/api/passkeys/'), 'Could not load your passkeys'))
}

export async function deletePasskey(id: number): Promise<void> {
  const { error, response } = await apiV2.DELETE('/api/passkeys/{passkey_id}', {
    params: { path: { passkey_id: id } },
  })
  if (error !== undefined || !response.ok) throw new Error(problemMessage(error, 'Could not delete the passkey'))
}

export async function getStepUpStatus(): Promise<StepUpStatusOut> {
  return need(await apiV2.GET('/api/passkeys/step-up'), 'Could not read the step-up status')
}

/** Register a passkey on THIS device (Touch ID, Windows Hello, a phone…). */
export async function registerThisDevice(label: string, opts: { upgrade?: boolean } = {}): Promise<PasskeyOut> {
  // `upgrade`: replace a passkey that cannot hold a vault key (no PRF) — the
  // server leaves it out of excludeCredentials so the passkey app makes the
  // new one for the same account.
  const begin = need(
    await apiV2.POST('/api/passkeys/register/begin', { params: { query: { upgrade: opts.upgrade === true } } }),
    'Could not start registering a passkey',
  )
  const publicKey = toCreationOptions(begin.options as Record<string, unknown>)
  // Ask for the PRF extension (canopy-web#1550): the answer, `prf.enabled`,
  // says whether this passkey can seal a zero-knowledge vault key. Nothing is
  // evaluated here, so no PRF output exists to leak.
  publicKey.extensions = { ...(publicKey.extensions ?? {}), prf: {} } as AuthenticationExtensionsClientInputs
  const created = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null
  if (!created) throw new Error('No passkey was created')
  const credential = credentialToJSON(created)
  const ext = credential.clientExtensionResults as { prf?: { enabled?: boolean } } | undefined
  // Only `enabled` goes to the server — never a PRF result, should one appear.
  credential.clientExtensionResults = ext?.prf ? { prf: { enabled: ext.prf.enabled === true } } : {}
  return need(
    await apiV2.POST('/api/passkeys/register/finish', { body: { credential, label } }),
    'The passkey could not be registered',
  )
}

/** Confirm with an existing passkey; opens the server's five-minute window. */
export async function performStepUp(): Promise<StepUpStatusOut> {
  const begin = need(await apiV2.POST('/api/passkeys/step-up/begin'), 'Could not start the passkey check')
  const asserted = (await navigator.credentials.get({
    publicKey: toRequestOptions(begin.options as Record<string, unknown>),
  })) as PublicKeyCredential | null
  if (!asserted) throw new Error('No passkey was used')
  return need(
    await apiV2.POST('/api/passkeys/step-up/finish', { body: { credential: credentialToJSON(asserted) } }),
    'The passkey could not be verified',
  )
}
