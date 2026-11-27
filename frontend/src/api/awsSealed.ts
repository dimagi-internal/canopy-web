/**
 * Sealed AWS roles (`apps/agents/aws_sealed.py`): the role keys canopy-web carries as ciphertext,
 * and a sealed grant's session, which the approver's browser makes and seals to the agent's machines.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

type S = components['schemas']
export type AwsSealedRole = S['AwsSealedRoleOut']
export type AwsSealedHolder = S['AwsSealedHolderOut']
export type AwsSealing = S['AwsSealingOut']
export type ZkRecipients = S['ZkRecipientsOut']

export async function listSealedRoles(): Promise<AwsSealedRole[]> {
  return Array.from(unwrap(await apiV2.GET('/api/aws/sealed-keys'), 'listSealedRoles')) as AwsSealedRole[]
}

export async function listSealedKeyHolders(): Promise<AwsSealedHolder[]> {
  return Array.from(unwrap(await apiV2.GET('/api/aws/sealed-keys/holders'), 'listSealedKeyHolders')) as AwsSealedHolder[]
}

export async function getAgentMachines(slug: string): Promise<ZkRecipients> {
  const res = await apiV2.GET('/api/aws/sealed-keys/machines/{slug}', { params: { path: { slug } } })
  return unwrap(res, 'getAgentMachines') as unknown as ZkRecipients
}

export async function putSealedKey(role: string, body: { access_key_id: string; value_zk: string; agent: string }) {
  const res = await apiV2.PUT('/api/aws/sealed-keys/{role}', { params: { path: { role } }, body })
  return unwrap(res, 'putSealedKey')
}

export async function deleteSealedKey(role: string, agent = '') {
  const res = await apiV2.DELETE('/api/aws/sealed-keys/{role}', { params: { path: { role }, query: { agent } } })
  return unwrap(res, 'deleteSealedKey')
}

export async function getSealing(slug: string, requestId: number): Promise<AwsSealing> {
  const res = await apiV2.GET('/api/agents/{slug}/aws-role-requests/{request_id}/sealing', {
    params: { path: { slug, request_id: requestId } },
  })
  return unwrap(res, 'getSealing') as AwsSealing
}

export async function putSealedSession(slug: string, requestId: number, body: { value_zk: string; expires_at: string }) {
  const res = await apiV2.PUT('/api/agents/{slug}/aws-role-requests/{request_id}/sealed-session', {
    params: { path: { slug, request_id: requestId } }, body,
  })
  return unwrap(res, 'putSealedSession')
}
