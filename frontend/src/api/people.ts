import { apiV2 } from './client.v2'
import type { components } from './generated'

export type PersonMe = components['schemas']['PersonMeOut']
export type PersonFactDetail = components['schemas']['PersonFactDetailOut']

/** Everything canopy holds about the signed-in user (fleet brain, canopy#804). */
export async function getMyPerson(): Promise<PersonMe> {
  const { data, error } = await apiV2.GET('/api/people/me/')
  if (error) throw new Error('Failed to load what agents know about you')
  return data as PersonMe
}

/** Your own agent-memory switch: on lets agents read and record what they learn
 *  about you; off stops every agent (nothing is deleted). Only you can flip it. */
export async function setMyAgentMemory(
  enabled: boolean,
): Promise<{ agent_memory: boolean; agent_memory_changed_at?: string | null }> {
  const { data, error } = await apiV2.PUT('/api/people/me/agent-memory/', {
    body: { enabled },
  })
  if (error) throw new Error('Failed to change agent memory')
  return data as { agent_memory: boolean; agent_memory_changed_at?: string | null }
}

export async function retractFact(personId: number, factId: number): Promise<void> {
  const { error } = await apiV2.POST('/api/people/{person_id}/facts/{fact_id}/retract/', {
    params: { path: { person_id: personId, fact_id: factId } },
  })
  if (error) throw new Error('Failed to retract that fact')
}

// --- HCP v1 (apps/contacts/hcp_api.py): the person's grants and audit log ------

/** One client's access to what canopy knows about you (HCP 4.1.5). */
export interface HcpGrant {
  grantId: string
  client: { id: string; name: string }
  scopes: string[]
  restrictions: unknown[]
  grantType: 'temporary' | 'persistent'
  grantor: string | null
  issuedAt: string
  expiresAt: string | null
  status: 'active' | 'revoked' | 'expired'
  canopy: { agent: string | null; channel: string; host: string; workspace: string; modality: string }
}

/** One event on your audit log (HCP 4.3.2). */
export interface HcpAuditEvent {
  eventId: string
  eventType: string
  timestamp: string
  actorId: string
  actorType: 'user' | 'agent' | 'system'
  entryId: string | null
  category: string | null
  purpose: string | null
  detail: string | null
}

export async function listMyGrants(status = 'all'): Promise<HcpGrant[]> {
  const { data, error } = await apiV2.GET('/api/hcp/v1/grants', { params: { query: { status } } })
  if (error) throw new Error('Failed to load who can read what about you')
  return ((data as unknown as { grants: HcpGrant[] }).grants ?? []) as HcpGrant[]
}

export async function revokeMyGrant(grantId: string): Promise<void> {
  const { error } = await apiV2.DELETE('/api/hcp/v1/grants/{grant_id}', {
    params: { path: { grant_id: grantId } },
  })
  if (error) throw new Error('Failed to revoke that access')
}

export async function listMyAudit(
  cursor?: string,
): Promise<{ events: HcpAuditEvent[]; nextCursor: string | null }> {
  const { data, error } = await apiV2.GET('/api/hcp/v1/audit', {
    params: { query: { limit: 50, ...(cursor ? { cursor } : {}) } },
  })
  if (error) throw new Error('Failed to load your audit log')
  return data as unknown as { events: HcpAuditEvent[]; nextCursor: string | null }
}
