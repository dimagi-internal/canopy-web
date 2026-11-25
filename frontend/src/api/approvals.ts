// Approval requests: a typed ask for one person's yes or no, with a hard-coded handler.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'
import { credentialToJSON, toRequestOptions } from '@/auth/webauthn'

export type ApprovalCard = components['schemas']['ApprovalCardOut']

export async function getApproval(id: number | string): Promise<ApprovalCard> {
  const res = await apiV2.GET('/api/approvals/{approval_id}', { params: { path: { approval_id: Number(id) } } })
  ensureOk(res, 'Failed to load that request')
  return res.data as ApprovalCard
}

export async function listApprovals(scope = ''): Promise<ApprovalCard[]> {
  const res = await apiV2.GET('/api/approvals/', { params: { query: scope ? { scope } : {} } })
  ensureOk(res, 'Failed to load your requests')
  return res.data as ApprovalCard[]
}

export async function decideApproval(
  id: number,
  decision: 'approve' | 'decline',
  comment: string,
): Promise<ApprovalCard> {
  const res = await apiV2.POST('/api/approvals/{approval_id}/decide', {
    params: { path: { approval_id: id } },
    body: { decision, comment },
  })
  ensureOk(res, decision === 'approve' ? 'Could not approve' : 'Could not decline')
  return res.data as ApprovalCard
}

/**
 * Approve a passkey kind (`step_up`): a WebAuthn assertion begun for THIS request, then the approval.
 * A passkey touched recently for something else does not count (`decide` refuses it).
 */
export async function approveWithPasskey(id: number, comment: string): Promise<ApprovalCard> {
  const begun = await apiV2.POST('/api/approvals/{approval_id}/approve/begin', {
    params: { path: { approval_id: id } },
  })
  ensureOk(begun, 'Could not start the passkey check')
  const { options } = begun.data as { options: Record<string, unknown> }
  const credential = (await navigator.credentials.get({
    publicKey: toRequestOptions(options),
  })) as PublicKeyCredential | null
  if (!credential) throw new Error('No passkey was used')
  const res = await apiV2.POST('/api/approvals/{approval_id}/approve/finish', {
    params: { path: { approval_id: id } },
    body: { credential: credentialToJSON(credential), comment },
  })
  ensureOk(res, 'Could not approve')
  return res.data as ApprovalCard
}
