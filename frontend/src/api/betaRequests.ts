// Requests for access to Canopy from the public site's form
// (apps/beta_requests). Read and answered by the person they are mailed to.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'

export type BetaRequestOut = components['schemas']['BetaRequestDetailOut']
export type BetaInviteRole = components['schemas']['BetaRequestInviteIn']['role']

export async function listBetaRequests(status?: string): Promise<BetaRequestOut[]> {
  const res = await apiV2.GET('/api/beta-requests', { params: { query: { status } } })
  ensureOk(res, 'Failed to load requests')
  return Array.from(res.data as unknown as BetaRequestOut[])
}

export async function getBetaRequest(id: number): Promise<BetaRequestOut> {
  const res = await apiV2.GET('/api/beta-requests/{request_id}', { params: { path: { request_id: id } } })
  ensureOk(res, 'Failed to load the request')
  return res.data as unknown as BetaRequestOut
}

export async function inviteBetaRequest(id: number, workspace: string, role: BetaInviteRole): Promise<BetaRequestOut> {
  const res = await apiV2.POST('/api/beta-requests/{request_id}/invite', {
    params: { path: { request_id: id } },
    body: { workspace, role },
  })
  ensureOk(res, 'Failed to send the invite')
  return res.data as unknown as BetaRequestOut
}

export async function declineBetaRequest(id: number): Promise<BetaRequestOut> {
  const res = await apiV2.POST('/api/beta-requests/{request_id}/decline', {
    params: { path: { request_id: id } },
  })
  ensureOk(res, 'Failed to decline')
  return res.data as unknown as BetaRequestOut
}
