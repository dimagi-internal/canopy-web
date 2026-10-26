/**
 * Act-as-you permissions (host grants): a connected site letting an agent act as
 * you, within your own access, on the page you sent from — taken only when you send
 * in that site's panel, for 30 minutes at a time. Listed and stopped here.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'

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
