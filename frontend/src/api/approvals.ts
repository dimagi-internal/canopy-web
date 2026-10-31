// Approval requests: a typed ask for one person's yes or no, with a hard-coded handler.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'

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
