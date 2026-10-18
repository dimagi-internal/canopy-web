// Mail domains API — a workspace's own domain run through SES (apps/mail).
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type MailDomainOut = components['schemas']['MailDomainOut']
export type MailRouteOut = components['schemas']['MailRouteOut']
export type DnsRecordOut = components['schemas']['DnsRecordOut']
export type MailDomainListOut = components['schemas']['MailDomainListOut']
export type MailRouteIn = components['schemas']['MailRouteIn']
export type MailThreadOut = components['schemas']['MailThreadOut']
export type MailThreadListOut = components['schemas']['MailThreadListOut']
export type MailMessageOut = components['schemas']['MailMessageOut']
export type AgentMailboxOut = components['schemas']['AgentMailboxOut']

// Same shape as InboundApiError: callers branch on the status (403 non-admin,
// 409 domain taken elsewhere, 422 reserved domain / reserved local part).
export class MailApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function fail(res: { response: Response; error?: unknown }, fallback: string): never {
  throw new MailApiError(res.response.status, problemMessage(res.error, fallback))
}

export async function listMailDomains(workspace: string): Promise<MailDomainListOut> {
  const res = await apiV2.GET('/api/mail/domains/{workspace}', { params: { path: { workspace } } })
  if (!res.response.ok) fail(res, 'Failed to load mail domains')
  return res.data as MailDomainListOut
}

export async function addMailDomain(workspace: string, name: string): Promise<MailDomainOut> {
  const res = await apiV2.POST('/api/mail/domains/{workspace}', { params: { path: { workspace } }, body: { name } })
  if (!res.response.ok) fail(res, 'Failed to add the domain')
  return res.data as MailDomainOut
}

export async function refreshMailDomain(workspace: string, domainId: number): Promise<MailDomainOut> {
  const res = await apiV2.POST('/api/mail/domains/{workspace}/{domain_id}/refresh', {
    params: { path: { workspace, domain_id: domainId } } })
  if (!res.response.ok) fail(res, 'Failed to re-check the domain')
  return res.data as MailDomainOut
}

export async function updateMailDomain(workspace: string, domainId: number,
  body: { daily_send_cap?: number; unsuspend?: boolean; sign_in?: boolean }): Promise<MailDomainOut> {
  const res = await apiV2.PATCH('/api/mail/domains/{workspace}/{domain_id}', {
    params: { path: { workspace, domain_id: domainId } }, body: { unsuspend: false, ...body } })
  if (!res.response.ok) fail(res, 'Failed to update the domain')
  return res.data as MailDomainOut
}

export async function removeMailDomain(workspace: string, domainId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}', {
    params: { path: { workspace, domain_id: domainId } } })
  if (!res.response.ok) fail(res, 'Failed to remove the domain')
}

export async function saveMailRoute(workspace: string, domainId: number, body: MailRouteIn): Promise<MailRouteOut> {
  const res = await apiV2.PUT('/api/mail/domains/{workspace}/{domain_id}/routes', {
    params: { path: { workspace, domain_id: domainId } }, body })
  if (!res.response.ok) fail(res, 'Failed to save the route')
  return res.data as MailRouteOut
}

export async function deleteMailRoute(workspace: string, domainId: number, routeId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}/routes/{route_id}', {
    params: { path: { workspace, domain_id: domainId, route_id: routeId } } })
  if (!res.response.ok) fail(res, 'Failed to remove the route')
}

// Superuser only: let `shareWith` and every workspace below it hold addresses on a
// domain owned by `workspace`.
export async function shareMailDomain(workspace: string, domainId: number,
  shareWith: string, dailySendCap?: number): Promise<MailDomainOut> {
  const res = await apiV2.PUT('/api/mail/domains/{workspace}/{domain_id}/shares', {
    params: { path: { workspace, domain_id: domainId } },
    body: { workspace: shareWith, daily_send_cap: dailySendCap ?? null } })
  if (!res.response.ok) fail(res, 'Failed to share the domain')
  return res.data as MailDomainOut
}

export async function unshareMailDomain(workspace: string, domainId: number,
  shareWorkspace: string): Promise<MailDomainOut> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}/shares/{share_workspace}', {
    params: { path: { workspace, domain_id: domainId, share_workspace: shareWorkspace } } })
  if (!res.response.ok) fail(res, 'Failed to stop sharing the domain')
  return res.data as MailDomainOut
}

export type MailThreadList = { items: MailThreadOut[]; agent_address: string }

// The agent's threads plus its own address here ('' when it has none).
export async function fetchMailThreads(workspace: string, agent?: string): Promise<MailThreadList> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}', {
    params: { path: { workspace }, query: agent ? { agent } : {} },
  })
  if (!res.response.ok) fail(res, 'Failed to load mail threads')
  const d = res.data as MailThreadListOut
  return { items: [...d.items], agent_address: d.agent_address ?? '' }
}

export async function fetchMailThread(workspace: string, threadId: string): Promise<MailThreadOut> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}/{uuid}', {
    params: { path: { workspace, uuid: threadId } },
  })
  if (!res.response.ok) fail(res, 'Failed to load mail thread')
  return res.data as MailThreadOut
}

/** Where an agent's address lives: canopy-hosted (nothing to sign in), Google, or none. */
export async function getAgentMailbox(slug: string): Promise<AgentMailboxOut> {
  const res = await apiV2.GET('/api/mail/agents/{slug}/mailbox', { params: { path: { slug } } })
  if (!res.response.ok) fail(res, 'Could not read the agent\'s mailbox')
  return res.data as AgentMailboxOut
}

/** Give an agent an address; on a canopy-hosted domain it is routed in the same step. */
export async function setAgentMailbox(slug: string, address: string): Promise<AgentMailboxOut> {
  const res = await apiV2.PUT('/api/mail/agents/{slug}/mailbox', { params: { path: { slug } }, body: { address } })
  if (!res.response.ok) fail(res, 'Could not save the address')
  return res.data as AgentMailboxOut
}
