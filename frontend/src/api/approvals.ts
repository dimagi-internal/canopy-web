// Approval cards: a proposal waiting on a yes or no, with everything needed to decide it.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'

export type ApprovalCard = components['schemas']['ApprovalCardOut']

export async function getApprovalCard(key: string): Promise<ApprovalCard> {
  const res = await apiV2.GET('/api/approvals/{key}', { params: { path: { key } } })
  ensureOk(res, 'Failed to load that card')
  return res.data as ApprovalCard
}

export async function listMyApprovals(): Promise<ApprovalCard[]> {
  const res = await apiV2.GET('/api/approvals/')
  ensureOk(res, 'Failed to load your cards')
  return res.data as ApprovalCard[]
}

export async function decideApproval(
  key: string,
  decision: 'approve' | 'decline',
  comment: string,
): Promise<ApprovalCard> {
  const res = await apiV2.POST('/api/approvals/{key}/decide', {
    params: { path: { key } },
    body: { decision, comment },
  })
  ensureOk(res, decision === 'approve' ? 'Could not approve' : 'Could not decline')
  return res.data as ApprovalCard
}
