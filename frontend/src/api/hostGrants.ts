/**
 * Act-as-you permissions (host grants): a connected site letting an agent act as
 * you, within your own access, on the page you sent from — taken only when you send
 * in that site's panel, for 30 minutes at a time. Listed and stopped here.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'
import { credentialToJSON, toRequestOptions } from '@/auth/webauthn'

export type HostGrant = components['schemas']['HostGrantOut']

// Branch on `res.response.ok`, never `res.error` (see tokens.ts).
export async function listMyHostGrants(): Promise<HostGrant[] | null> {
  const res = await apiV2.GET('/api/me/host-grants/')
  if (!res.response.ok || !res.data) return null
  return res.data as unknown as HostGrant[]
}

export async function stopMyHostGrant(id: number): Promise<boolean> {
  const res = await apiV2.DELETE('/api/me/host-grants/{grant_id}', {
    params: { path: { grant_id: id } },
  })
  return res.response.ok
}

/** One of your unexpired permissions, confirmed or still waiting for your passkey. */
export async function getMyHostGrant(id: number): Promise<HostGrant> {
  const res = await apiV2.GET('/api/me/host-grants/{grant_id}', { params: { path: { grant_id: id } } })
  return unwrap(res, 'Could not load this permission') as HostGrant
}

/** Confirm a page's waiting permission with a passkey, here in the browser — for a
 *  panel that cannot use one itself (the Canopy Runner app; `auth/passkeyHere.ts`). */
export async function confirmMyHostGrant(id: number): Promise<HostGrant> {
  const path = { params: { path: { grant_id: id } } }
  const begun = unwrap(await apiV2.POST('/api/me/host-grants/{grant_id}/confirm/begin', path),
    'Could not start the passkey check') as { options: Record<string, unknown> }
  const credential = (await navigator.credentials.get({
    publicKey: toRequestOptions(begun.options),
  })) as PublicKeyCredential | null
  if (!credential) throw new Error('No passkey was used')
  const res = await apiV2.POST('/api/me/host-grants/{grant_id}/confirm/finish', {
    ...path,
    body: { credential: credentialToJSON(credential) },
  })
  return unwrap(res, 'The passkey could not be verified') as HostGrant
}
