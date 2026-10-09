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

export type AgentMemory = components['schemas']['AgentMemoryOut']
export type MemoryFeature = 'record' | 'use'
export type SessionMemory = components['schemas']['SessionMemoryOut']

/** Your agent memory at the canopy level, per feature: `available` — may it be on
 *  at all — and `default` — is it on in a new session. `record` lets agents learn
 *  about you (write); `use` lets them be told what they learned (read). Off
 *  deletes nothing. Only you can change these. Omit a key to leave it. */
export async function setMyAgentMemory(
  change: Partial<Record<MemoryFeature, { available?: boolean; default?: boolean }>>,
): Promise<AgentMemory> {
  const { data, error } = await apiV2.PUT('/api/people/me/agent-memory/', { body: change })
  if (error) throw new Error('Failed to change agent memory')
  return data as AgentMemory
}

/** Agent memory as it applies in one of your sessions. Throws on 404 (not your
 *  session) — callers treat that as "show nothing". */
export async function getSessionMemory(sessionId: string): Promise<SessionMemory> {
  const { data, error } = await apiV2.GET('/api/people/me/sessions/{session_id}/agent-memory/', {
    params: { path: { session_id: sessionId } },
  })
  if (error) throw new Error('Failed to load agent memory for this session')
  return data as SessionMemory
}

/** Turn a feature on or off for this session only, or back to your default. */
export async function setSessionMemory(
  sessionId: string,
  change: Partial<Record<MemoryFeature, 'on' | 'off' | 'inherit'>>,
): Promise<SessionMemory> {
  const { data, error } = await apiV2.PUT('/api/people/me/sessions/{session_id}/agent-memory/', {
    params: { path: { session_id: sessionId } },
    body: change,
  })
  if (error) throw new Error('Failed to change agent memory for this session')
  return data as SessionMemory
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
  canopy: {
    agent: string | null
    channel: string
    host: string
    workspace: string | null
    modality: string
    /** The OAuth client the grant was issued to, when it was one (HCP service). */
    client?: { id: string; name: string; operator: string } | null
  }
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

// --- your own entries (HCP 3.2 / 3.3, as the person) -----------------------------

/** The categories you can file something about yourself under (canopy holds work
 *  context only — PersonFact.CATEGORIES). */
export const MY_CATEGORIES = [
  { value: 'general_preferences', label: 'How I like to work' },
  { value: 'work_context', label: 'My work and role' },
  { value: 'goals_and_constraints', label: 'My goals and constraints' },
  { value: 'coordination_context', label: 'How to coordinate with me' },
] as const

/** Add a PERSONAL entry — yours, in no workspace. No agent in a workspace reads a
 *  personal entry; you do, and any client you authorize does. */
export async function addMyEntry(category: string, preference: string): Promise<void> {
  const { error } = await apiV2.POST('/api/hcp/v1/preferences/add', {
    body: {
      category,
      preference,
      declarationType: 'user-declared',
      sourceContext: 'user-input',
    },
  })
  if (error) throw new Error('Failed to save that')
}

/** Correct an entry: a new version, same id (HCP 3.2.3). Correcting an agent's
 *  inference makes it yours (2.6.3). */
export async function correctMyEntry(entryId: string, text: string): Promise<void> {
  const { error } = await apiV2.PUT('/api/hcp/v1/preferences/{entry_id}', {
    params: { path: { entry_id: entryId } },
    body: { updatedPreference: text, reason: 'corrected by the person on /people/me' },
  })
  if (error) throw new Error('Failed to save the correction')
}

/** Everything canopy holds about you, as one JSON-LD file (HCP 3.3.5), with every
 *  version and your audit log. */
export async function exportMine(): Promise<Blob> {
  const { data, error } = await apiV2.GET('/api/hcp/v1/export', {
    params: { query: { include: 'audit,versions' } },
  })
  if (error) throw new Error('Failed to export')
  return new Blob([JSON.stringify(data, null, 2)], { type: 'application/ld+json' })
}

// --- the HCP service's client registry (apps/contacts/hcp_clients_api.py) ---------
// Superusers only. Apps canopy does not operate that may ask people for access.

export interface HcpClientRow {
  client_id: string
  name: string
  operator: string
  description: string
  redirect_uris: string[]
  allowed_scopes: string[]
  first_party: boolean
  webhook_url: string
  created_at: string
  disabled_at: string | null
  active_grants: number
  webhook_secret?: string | null
}

export type HcpClientInput = components['schemas']['HcpClientIn']
export type HcpClientPatch = components['schemas']['HcpClientPatch']

function registryError(error: unknown, what: string): Error {
  const e = error as { status?: number; detail?: string; title?: string } | undefined
  if (e?.status === 403) return new Error('Only canopy administrators manage HCP clients.')
  return new Error(`${what}${e?.detail ? `: ${e.detail}` : ''}`)
}

export async function listHcpClients(): Promise<HcpClientRow[]> {
  const { data, error } = await apiV2.GET('/api/hcp-admin/clients')
  if (error) throw registryError(error, 'Failed to load clients')
  return data as unknown as HcpClientRow[]
}

export async function registerHcpClient(body: HcpClientInput): Promise<HcpClientRow> {
  const { data, error } = await apiV2.POST('/api/hcp-admin/clients', { body })
  if (error) throw registryError(error, 'Failed to register the client')
  return data as unknown as HcpClientRow
}

export async function updateHcpClient(
  clientId: string,
  body: HcpClientPatch,
): Promise<HcpClientRow> {
  const { data, error } = await apiV2.PATCH('/api/hcp-admin/clients/{client_id}', {
    params: { path: { client_id: clientId } },
    body,
  })
  if (error) throw registryError(error, 'Failed to update the client')
  return data as unknown as HcpClientRow
}
