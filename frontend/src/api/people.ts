import { apiV2 } from './client.v2'
import type { components } from './generated'
import { ApiError, ensureOk, unwrap } from './problem'

export type PersonMe = components['schemas']['PersonMeOut']
export type PersonFactDetail = components['schemas']['PersonFactDetailOut']

/** Everything canopy holds about the signed-in user (fleet brain, canopy#804). */
export async function getMyPerson(): Promise<PersonMe> {
  const res = await apiV2.GET('/api/people/me/')
  const data = unwrap(res, 'Failed to load what agents know about you')
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
  const res = await apiV2.PUT('/api/people/me/agent-memory/', { body: change })
  const data = unwrap(res, 'Failed to change agent memory')
  return data as AgentMemory
}

/** Agent memory as it applies in one of your sessions. Throws on 404 (not your
 *  session) — callers treat that as "show nothing". */
export async function getSessionMemory(sessionId: string): Promise<SessionMemory> {
  const res = await apiV2.GET('/api/people/me/sessions/{session_id}/agent-memory/', {
    params: { path: { session_id: sessionId } },
  })
  const data = unwrap(res, 'Failed to load agent memory for this session')
  return data as SessionMemory
}

/** Turn a feature on or off for this session only, or back to your default. */
export async function setSessionMemory(
  sessionId: string,
  change: Partial<Record<MemoryFeature, 'on' | 'off' | 'inherit'>>,
): Promise<SessionMemory> {
  const res = await apiV2.PUT('/api/people/me/sessions/{session_id}/agent-memory/', {
    params: { path: { session_id: sessionId } },
    body: change,
  })
  const data = unwrap(res, 'Failed to change agent memory for this session')
  return data as SessionMemory
}

export type GrantDuration = 'session' | 'always'

/** Your act (HCP 4.1.6) in one of your sessions, for its agent: `session` allows
 *  it for this session (temporary — the default outcome); `always` then keeps it
 *  for that agent until you revoke it, and is refused unless this session already
 *  allows it (4.1.4: one act never both allows and keeps). Show the agent, the
 *  categories, the actions and the duration first. */
export async function grantSessionAgent(
  sessionId: string,
  features: MemoryFeature[],
  duration: GrantDuration,
  surface: 'chat' | 'widget' = 'chat',
): Promise<SessionMemory> {
  const res = await apiV2.POST('/api/people/me/sessions/{session_id}/agent-grants/', {
    params: { path: { session_id: sessionId } },
    body: { features, duration, surface },
  })
  const data = unwrap(res, 'Failed to grant this agent')
  return data as SessionMemory
}

export async function retractFact(personId: number, factId: number): Promise<void> {
  const res = await apiV2.POST('/api/people/{person_id}/facts/{fact_id}/retract/', {
    params: { path: { person_id: personId, fact_id: factId } },
  })
  ensureOk(res, 'Failed to retract that fact')
}

// --- More ways to reach a person (PersonAddress) ------------------------------

export type PersonAddress = components['schemas']['PersonAddressOut']
export type AddressKind = 'email' | 'phone'

/** Your own contact details — yours across every workspace. */
export async function listMyAddresses(): Promise<PersonAddress[]> {
  return [...((await getMyPerson()).addresses ?? [])]
}

/** Add one of yours; only you change your own. The same kind and value again
 *  relabels it. Rejects with the server's reason (not an email, …). */
export async function addMyAddress(body: { kind: AddressKind; label: string; value: string }): Promise<PersonAddress> {
  const res = await apiV2.POST('/api/people/me/addresses/', { body })
  const data = unwrap(res, 'Failed to add that address')
  return data as PersonAddress
}

export async function removeMyAddress(addressId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/people/me/addresses/{address_id}/', {
    params: { path: { address_id: addressId } },
  })
  ensureOk(res, 'Failed to remove that address')
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
    /** An agent's grant: the part of your memory it reaches, in words (HCP 4.1.5.1). */
    reach?: string
    /** The same reach as data: the workspaces it reads, `null` = your personal entries. */
    reaches?: (string | null)[]
    /** The same, resolved now as workspace names ("No workspace" last). */
    reach_names?: string[]
    /** The memory collection it uses; null = the agent's default (its organisation). */
    collection?: { id: number; name: string } | null
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
  const res = await apiV2.GET('/api/hcp/v1/grants', { params: { query: { status } } })
  const data = unwrap(res, 'Failed to load who can read what about you')
  return ((data as unknown as { grants: HcpGrant[] }).grants ?? []) as HcpGrant[]
}

export async function revokeMyGrant(grantId: string): Promise<void> {
  const res = await apiV2.DELETE('/api/hcp/v1/grants/{grant_id}', {
    params: { path: { grant_id: grantId } },
  })
  ensureOk(res, 'Failed to revoke that access')
}

export type MemoryCollections = components['schemas']['MemoryCollectionsOut']
export type MemoryCollection = components['schemas']['MemoryCollectionOut']

/** A change that would let agents reach more: canopy refuses it until you confirm,
 *  and says which agents and what they would newly reach. Resend with `grants`. */
export class NeedsConfirm extends Error {
  grants: string[]
  agents: string[]
  adds: string

  constructor(grants: string[], agents: string[], adds: string) {
    super(`This will also let ${agents.join(' and ')} use ${adds}.`)
    this.grants = grants
    this.agents = agents
    this.adds = adds
  }
}

function confirmOrThrow(status: number, error: unknown, fallback: string): never {
  const extras = (error as { extras?: { confirm_widening?: string[]; agents?: string[]; adds?: string } })
    ?.extras
  if (extras?.confirm_widening) {
    throw new NeedsConfirm(extras.confirm_widening, extras.agents ?? [], extras.adds ?? '')
  }
  throw ApiError.from(status, error, fallback)
}

/** Your memory collections — named sets of contexts (all of an organisation, one
 *  workspace, entries in no workspace) you can point an agent at — and the
 *  contexts. Resolved when read: "all of" an organisation takes in new workspaces. */
export async function listMyCollections(): Promise<MemoryCollections> {
  const res = await apiV2.GET('/api/people/me/memory-collections/')
  const data = unwrap(res, 'Failed to load your collections')
  return data as MemoryCollections
}

export async function createMyCollection(name: string, sources: string[]): Promise<MemoryCollection> {
  const { data, error, response } = await apiV2.POST('/api/people/me/memory-collections/', {
    body: { name, sources },
  })
  if (!response.ok) confirmOrThrow(response.status, error, 'Failed to make that collection')
  return data as MemoryCollection
}

/** Change what a collection holds. Adding a context throws `NeedsConfirm` until
 *  you pass back the grants it named. */
export async function updateMyCollection(
  id: number,
  change: { name?: string; sources?: string[] },
  confirmWidening?: string[],
): Promise<MemoryCollection> {
  const { data, error, response } = await apiV2.PUT('/api/people/me/memory-collections/{collection_id}/', {
    params: { path: { collection_id: id } },
    body: { ...change, ...(confirmWidening ? { confirm_widening: confirmWidening } : {}) },
  })
  if (!response.ok) confirmOrThrow(response.status, error, 'Failed to change that collection')
  return data as MemoryCollection
}

export async function deleteMyCollection(id: number): Promise<void> {
  const { error, response } = await apiV2.DELETE('/api/people/me/memory-collections/{collection_id}/', {
    params: { path: { collection_id: id } },
  })
  if (!response.ok) confirmOrThrow(response.status, error, 'Failed to remove that collection')
}

/** Which collection an agent you allowed uses (`null` = its default, its own
 *  organisation). Throws `NeedsConfirm` when that lets it reach more. */
export async function setMyGrantCollection(
  grantId: string,
  collectionId: number | null,
  confirmWidening?: string[],
): Promise<MemoryCollections> {
  const { data, error, response } = await apiV2.PUT('/api/people/me/grants/{grant_id}/collection/', {
    params: { path: { grant_id: grantId } },
    body: { collection_id: collectionId, ...(confirmWidening ? { confirm_widening: confirmWidening } : {}) },
  })
  if (!response.ok) confirmOrThrow(response.status, error, 'Failed to change what that agent reaches')
  return data as MemoryCollections
}

export async function listMyAudit(
  cursor?: string,
): Promise<{ events: HcpAuditEvent[]; nextCursor: string | null }> {
  const res = await apiV2.GET('/api/hcp/v1/audit', {
    params: { query: { limit: 50, ...(cursor ? { cursor } : {}) } },
  })
  const data = unwrap(res, 'Failed to load your audit log')
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

/** Add an entry of your own to a workspace you belong to; agents whose reach
 *  covers that workspace read it. `workspace` null saves it in no workspace —
 *  only for someone who belongs to none. */
export async function addMyEntry(category: string, preference: string, workspace: string | null = null): Promise<void> {
  const res = await apiV2.POST('/api/hcp/v1/preferences/add', {
    params: { query: workspace ? { workspace } : {} },
    body: {
      category,
      preference,
      declarationType: 'user-declared',
      sourceContext: 'user-input',
    },
  })
  ensureOk(res, 'Failed to save that')
}

/** Correct an entry: a new version, same id (HCP 3.2.3). Correcting an agent's
 *  inference makes it yours (2.6.3). */
export async function correctMyEntry(entryId: string, text: string): Promise<void> {
  const res = await apiV2.PUT('/api/hcp/v1/preferences/{entry_id}', {
    params: { path: { entry_id: entryId } },
    body: { updatedPreference: text, reason: 'corrected by the person on /people/me' },
  })
  ensureOk(res, 'Failed to save the correction')
}

/** Everything canopy holds about you, as one JSON-LD file (HCP 3.3.5), with every
 *  version and your audit log. */
export async function exportMine(): Promise<Blob> {
  const res = await apiV2.GET('/api/hcp/v1/export', {
    params: { query: { include: 'audit,versions' } },
  })
  const data = unwrap(res, 'Failed to export')
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

function registryError(status: number, error: unknown, what: string): ApiError {
  if (status === 403) return new ApiError(403, 'Only canopy administrators manage HCP clients.', error)
  return ApiError.from(status, error, what)
}

export async function listHcpClients(): Promise<HcpClientRow[]> {
  const { data, error, response } = await apiV2.GET('/api/hcp-admin/clients')
  if (!response.ok) throw registryError(response.status, error, 'Failed to load clients')
  return data as unknown as HcpClientRow[]
}

export async function registerHcpClient(body: HcpClientInput): Promise<HcpClientRow> {
  const { data, error, response } = await apiV2.POST('/api/hcp-admin/clients', { body })
  if (!response.ok) throw registryError(response.status, error, 'Failed to register the client')
  return data as unknown as HcpClientRow
}

export async function updateHcpClient(
  clientId: string,
  body: HcpClientPatch,
): Promise<HcpClientRow> {
  const { data, error, response } = await apiV2.PATCH('/api/hcp-admin/clients/{client_id}', {
    params: { path: { client_id: clientId } },
    body,
  })
  if (!response.ok) throw registryError(response.status, error, 'Failed to update the client')
  return data as unknown as HcpClientRow
}

export type SearchPreview = components['schemas']['SearchPreviewOut']
export type SearchPreviewEntry = SearchPreview['entries'][number]

/** Score every entry you hold against a question the way an agent's search does,
 *  with the reason each would or would not come back. `floor` previews another
 *  cutoff; agents keep the live one. Changes nothing. */
export async function previewMySearch(query: string, floor?: number): Promise<SearchPreview> {
  const res = await apiV2.POST('/api/people/me/memory-search-preview/', {
    body: { query, floor: floor ?? null, limit: 8 },
  })
  const data = unwrap(res, 'Search preview failed')
  return data as SearchPreview
}

export type SearchRecord = components['schemas']['SearchRecordOut']
export type SearchHistory = components['schemas']['SearchHistoryOut']

/** Every search agents ran over your memory, newest first, as it ran: the
 *  question, every entry scored, and the settings then. `before` pages back. */
export async function listMySearches(before?: string): Promise<SearchHistory> {
  const res = await apiV2.GET('/api/people/me/memory-searches/', {
    params: { query: { before: before ?? null, limit: 30 } },
  })
  const data = unwrap(res, 'Failed to load how agents searched your memory')
  return data as SearchHistory
}

export type MemoryViewers = components['schemas']['MemoryViewersOut']

/** The agents you let see this page, read-only, and the ones you could let in. */
export async function listMyViewers(): Promise<MemoryViewers> {
  const res = await apiV2.GET('/api/people/me/viewers/')
  const data = unwrap(res, 'Failed to load which agents can see this page')
  return data as MemoryViewers
}

export async function addMyViewer(agent: string): Promise<MemoryViewers> {
  const res = await apiV2.POST('/api/people/me/viewers/', { body: { agent } })
  const data = unwrap(res, 'Failed to let that agent see this page')
  return data as MemoryViewers
}

export async function removeMyViewer(agent: string): Promise<MemoryViewers> {
  const res = await apiV2.DELETE('/api/people/me/viewers/{agent_slug}/', {
    params: { path: { agent_slug: agent } },
  })
  const data = unwrap(res, 'Failed to stop that agent seeing this page')
  return data as MemoryViewers
}

// --- someone's page, as they let this agent see it (apps/contacts/viewers.py) -----

export type MemoryPage = components['schemas']['MemoryPageOut']

/** A person's memory page for an agent whose own login they let see it.
 *  Read-only; each look is in their audit. 404 for anyone else. */
export async function getPersonPage(personId: number): Promise<MemoryPage> {
  const res = await apiV2.GET('/api/people/{person_id}/page/', {
    params: { path: { person_id: personId } },
  })
  const data = unwrap(res, 'This page isn’t shared with you')
  return data as MemoryPage
}

export async function listPersonPageAudit(
  personId: number,
  cursor?: string,
): Promise<{ events: HcpAuditEvent[]; nextCursor: string | null }> {
  const res = await apiV2.GET('/api/people/{person_id}/page/audit/', {
    params: { path: { person_id: personId }, query: cursor ? { cursor } : {} },
  })
  const data = unwrap(res, 'Failed to load what happened')
  return data as unknown as { events: HcpAuditEvent[]; nextCursor: string | null }
}

export async function listPersonPageSearches(personId: number, before?: string): Promise<SearchHistory> {
  const res = await apiV2.GET('/api/people/{person_id}/page/searches/', {
    params: { path: { person_id: personId }, query: { before: before ?? null, limit: 30 } },
  })
  const data = unwrap(res, 'Failed to load how agents searched it')
  return data as SearchHistory
}
