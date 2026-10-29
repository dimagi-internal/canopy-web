/**
 * Act-as-you on canopy (`apps/harness/act_as.py`): an agent, in a turn you started,
 * asking to act as you on canopy-web for 30 minutes — read-only or read + write —
 * which you approve with a passkey touch made for that one request.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'
import { credentialToJSON, toRequestOptions } from '@/auth/webauthn'

export type ActAsRequest = components['schemas']['ActAsRequestOut']
export type ActAsScope = 'read' | 'write'

export async function listMyActAsRequests(): Promise<ActAsRequest[]> {
  const res = await apiV2.GET('/api/me/act-as-requests/')
  return Array.from(unwrap(res, 'listMyActAsRequests')) as ActAsRequest[]
}

export async function getActAsRequest(id: string): Promise<ActAsRequest> {
  const res = await apiV2.GET('/api/me/act-as-requests/{request_id}', {
    params: { path: { request_id: id } },
  })
  return unwrap(res, 'getActAsRequest') as ActAsRequest
}

/** Approve with a passkey: a WebAuthn assertion begun for THIS request, then the grant. */
export async function approveActAsRequest(id: string, scope: ActAsScope): Promise<ActAsRequest> {
  const begun = await apiV2.POST('/api/me/act-as-requests/{request_id}/approve/begin', {
    params: { path: { request_id: id } },
  })
  const { options } = unwrap(begun, 'approveActAsRequest') as { options: Record<string, unknown> }
  const credential = (await navigator.credentials.get({
    publicKey: toRequestOptions(options),
  })) as PublicKeyCredential | null
  if (!credential) throw new Error('No passkey was used')
  const res = await apiV2.POST('/api/me/act-as-requests/{request_id}/approve/finish', {
    params: { path: { request_id: id } },
    body: { credential: credentialToJSON(credential), scope },
  })
  return unwrap(res, 'approveActAsRequest') as ActAsRequest
}

export async function declineActAsRequest(id: string): Promise<ActAsRequest> {
  const res = await apiV2.POST('/api/me/act-as-requests/{request_id}/decline', {
    params: { path: { request_id: id } },
  })
  return unwrap(res, 'declineActAsRequest') as ActAsRequest
}

export async function stopActAsRequest(id: string): Promise<ActAsRequest> {
  const res = await apiV2.POST('/api/me/act-as-requests/{request_id}/stop', {
    params: { path: { request_id: id } },
  })
  return unwrap(res, 'stopActAsRequest') as ActAsRequest
}

/** The buttons a request offers: what the agent asked for, read-only first. */
export function offeredScopes(requested: string): ActAsScope[] {
  if (requested === 'read') return ['read']
  if (requested === 'write') return ['write']
  return ['read', 'write']
}

/** What happened to the request, in plain words, with times. */
export function statusLine(req: ActAsRequest, agent: string, now: number = Date.now()): string {
  const at = when(req.expires_at)
  const past = req.expires_at ? new Date(req.expires_at).getTime() <= now : false
  switch (req.status) {
    case 'pending':
      return `Waiting for you — decide by ${at}.`
    case 'approved':
      return `Approved, ${req.scope === 'write' ? 'read + write' : 'read-only'} until ${at}.`
    case 'declined':
      return 'You declined this request.'
    case 'stopped':
      return req.approved_at ? 'This permission was stopped.' : 'This request was withdrawn.'
    case 'expired':
      if (req.approved_at) {
        return past ? `This permission ended at ${at}.` : 'This permission ended when its conversation did.'
      }
      return past
        ? `This request expired at ${at} — ask ${agent} again.`
        : `This request ended when its conversation did — ask ${agent} again.`
    default:
      return req.status
  }
}

function when(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}
