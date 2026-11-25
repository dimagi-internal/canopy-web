/**
 * Act-as-you on canopy (`apps/harness/act_as.py`): an agent, in a turn you started,
 * asking to act as you on canopy-web for 30 minutes — read-only or read + write. Asking
 * files an approval addressed to you alone (`approval_url`, `/approvals/:id`), which you
 * approve there with a passkey touch made for that one request, like every approval.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type ActAsRequest = components['schemas']['ActAsRequestOut']

export async function listMyActAsRequests(): Promise<ActAsRequest[]> {
  const res = await apiV2.GET('/api/me/act-as-requests/')
  return Array.from(unwrap(res, 'listMyActAsRequests')) as ActAsRequest[]
}

export async function stopActAsRequest(id: string): Promise<ActAsRequest> {
  const res = await apiV2.POST('/api/me/act-as-requests/{request_id}/stop', {
    params: { path: { request_id: id } },
  })
  return unwrap(res, 'stopActAsRequest') as ActAsRequest
}
