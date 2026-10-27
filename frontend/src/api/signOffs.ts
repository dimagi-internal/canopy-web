// Site sign-offs: a connected site asks you to approve one action with your passkey
// (`apps/host_grants/sign_offs_api.py`). The passkey step-up is the API client's: a 403
// `step_up_required` prompts for the passkey and retries the approve.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

type Schemas = components['schemas']

export type SignOffOut = Schemas['SignOffOut']
export type SignOffDecidedOut = Schemas['SignOffDecidedOut']

export async function getSignOff(id: string): Promise<SignOffOut> {
  const res = await apiV2.GET('/api/sign-offs/{sign_off_id}', { params: { path: { sign_off_id: id } } })
  return unwrap(res, 'Could not load the request')
}

/** Approve (browser + passkey step-up, answered by the API client) or decline. */
export async function decideSignOff(id: string, approve: boolean): Promise<SignOffDecidedOut> {
  const params = { params: { path: { sign_off_id: id } } }
  const res = approve
    ? await apiV2.POST('/api/sign-offs/{sign_off_id}/approve', params)
    : await apiV2.POST('/api/sign-offs/{sign_off_id}/decline', params)
  return unwrap(res, approve ? 'Could not approve' : 'Could not decline')
}
