// Mail domains API — a workspace's own domain run through SES (apps/mail).
import { apiV2 } from './client.v2'
import { ApiError, ensureOk } from './problem'
import type { components } from './generated'

export type MailDomainOut = components['schemas']['MailDomainOut']
export type MailRouteOut = components['schemas']['MailRouteOut']
export type DnsRecordOut = components['schemas']['DnsRecordOut']
export type MailDomainListOut = components['schemas']['MailDomainListOut']
export type MailRouteIn = components['schemas']['MailRouteIn']
export type MailThreadOut = components['schemas']['MailThreadOut']
export type MailThreadListOut = components['schemas']['MailThreadListOut']
export type MailMessageOut = components['schemas']['MailMessageOut']
export type MailSendIn = components['schemas']['MailSendIn']
export type MailInboundLogOut = components['schemas']['MailInboundLogOut']
export type MailInboundLogListOut = components['schemas']['MailInboundLogListOut']
export type AgentMailboxOut = components['schemas']['AgentMailboxOut']

// Same shape as InboundApiError: callers branch on the status (403 non-admin,
// 409 domain taken elsewhere, 422 reserved domain / reserved local part).
export { ApiError as MailApiError }


export async function listMailDomains(workspace: string): Promise<MailDomainListOut> {
  const res = await apiV2.GET('/api/mail/domains/{workspace}', { params: { path: { workspace } } })
  ensureOk(res, 'Failed to load mail domains')
  return res.data as MailDomainListOut
}

export async function addMailDomain(workspace: string, name: string): Promise<MailDomainOut> {
  const res = await apiV2.POST('/api/mail/domains/{workspace}', { params: { path: { workspace } }, body: { name } })
  ensureOk(res, 'Failed to add the domain')
  return res.data as MailDomainOut
}

export async function refreshMailDomain(workspace: string, domainId: number): Promise<MailDomainOut> {
  const res = await apiV2.POST('/api/mail/domains/{workspace}/{domain_id}/refresh', {
    params: { path: { workspace, domain_id: domainId } } })
  ensureOk(res, 'Failed to re-check the domain')
  return res.data as MailDomainOut
}

export async function updateMailDomain(workspace: string, domainId: number,
  body: { daily_send_cap?: number; unsuspend?: boolean; sign_in?: boolean; agent_addresses?: boolean }): Promise<MailDomainOut> {
  const res = await apiV2.PATCH('/api/mail/domains/{workspace}/{domain_id}', {
    params: { path: { workspace, domain_id: domainId } }, body: { unsuspend: false, ...body } })
  ensureOk(res, 'Failed to update the domain')
  return res.data as MailDomainOut
}

export async function removeMailDomain(workspace: string, domainId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}', {
    params: { path: { workspace, domain_id: domainId } } })
  ensureOk(res, 'Failed to remove the domain')
}

export async function saveMailRoute(workspace: string, domainId: number, body: MailRouteIn): Promise<MailRouteOut> {
  const res = await apiV2.PUT('/api/mail/domains/{workspace}/{domain_id}/routes', {
    params: { path: { workspace, domain_id: domainId } }, body })
  ensureOk(res, 'Failed to save the route')
  return res.data as MailRouteOut
}

export async function deleteMailRoute(workspace: string, domainId: number, routeId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}/routes/{route_id}', {
    params: { path: { workspace, domain_id: domainId, route_id: routeId } } })
  ensureOk(res, 'Failed to remove the route')
}

// Superuser only: let `shareWith` and every workspace below it hold addresses on a
// domain owned by `workspace`.
export async function shareMailDomain(workspace: string, domainId: number,
  shareWith: string, dailySendCap?: number): Promise<MailDomainOut> {
  const res = await apiV2.PUT('/api/mail/domains/{workspace}/{domain_id}/shares', {
    params: { path: { workspace, domain_id: domainId } },
    body: { workspace: shareWith, daily_send_cap: dailySendCap ?? null } })
  ensureOk(res, 'Failed to share the domain')
  return res.data as MailDomainOut
}

export async function unshareMailDomain(workspace: string, domainId: number,
  shareWorkspace: string): Promise<MailDomainOut> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}/shares/{share_workspace}', {
    params: { path: { workspace, domain_id: domainId, share_workspace: shareWorkspace } } })
  ensureOk(res, 'Failed to stop sharing the domain')
  return res.data as MailDomainOut
}

export async function fetchMailThread(workspace: string, threadId: string): Promise<MailThreadOut> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}/{uuid}', {
    params: { path: { workspace, uuid: threadId } },
  })
  ensureOk(res, 'Failed to load mail thread')
  return res.data as MailThreadOut
}

/** Where an agent's address lives: canopy-hosted (nothing to sign in), Google, or none. */
export async function getAgentMailbox(slug: string): Promise<AgentMailboxOut> {
  const res = await apiV2.GET('/api/mail/agents/{slug}/mailbox', { params: { path: { slug } } })
  ensureOk(res, 'Could not read the agent\'s mailbox')
  return res.data as AgentMailboxOut
}

/** Give an agent an address; on a canopy-hosted domain it is routed in the same step. */
export async function setAgentMailbox(slug: string, address: string): Promise<AgentMailboxOut> {
  const res = await apiV2.PUT('/api/mail/agents/{slug}/mailbox', { params: { path: { slug } }, body: { address } })
  ensureOk(res, 'Could not save the address')
  return res.data as AgentMailboxOut
}

/** Every message that reached a domain and what became of it (admin or owner). */
export async function fetchInboundLog(workspace: string, domainId: number, recipient = ''): Promise<MailInboundLogListOut> {
  const res = await apiV2.GET('/api/mail/domains/{workspace}/{domain_id}/inbound', {
    params: { path: { workspace, domain_id: domainId }, query: recipient ? { recipient } : {} },
  })
  ensureOk(res, 'Failed to load the inbound mail log')
  return res.data as MailInboundLogListOut
}

/** Mark a thread read/unread, or move it: archived, spam, trashed. */
export async function setMailThreadState(workspace: string, threadId: string,
  body: { unread?: boolean; archived?: boolean; spam?: boolean; trashed?: boolean }): Promise<MailThreadOut> {
  const res = await apiV2.POST('/api/mail/threads/{workspace}/{uuid}/state', {
    params: { path: { workspace, uuid: threadId } }, body })
  ensureOk(res, 'Failed to update the thread')
  return res.data as MailThreadOut
}

export type MailFolder = 'inbox' | 'sent' | 'archived' | 'spam' | 'trash'
export type MailInbox = { items: MailThreadOut[]; unread_total: number; addresses: string[]; agent_address: string; has_more: boolean }
export type InboxSource = { agent?: string; forwarded?: boolean }
export type InboxFilter = { folder?: MailFolder; q?: string; address?: string; offset?: number; limit?: number }
export type MailUploadOut = components['schemas']['MailUploadOut']
export type MailDraftOut = components['schemas']['MailDraftOut']
export type MailDraftIn = components['schemas']['MailDraftIn']

/** One inbox's threads: an agent's (`agent`) or the caller's own kept forwarded mail (`forwarded`),
 *  by folder, search text or address. `addresses` are what it can send from. */
export async function fetchInbox(workspace: string, source: InboxSource, opts: InboxFilter = {}): Promise<MailInbox> {
  const query: Record<string, string | boolean | number> = {}
  if (source.agent) query.agent = source.agent
  if (source.forwarded) query.forwarded = true
  if (opts.folder) query.folder = opts.folder
  if (opts.q?.trim()) query.q = opts.q.trim()
  if (opts.address) query.address = opts.address
  if (opts.offset) query.offset = opts.offset
  if (opts.limit) query.limit = opts.limit
  const res = await apiV2.GET('/api/mail/threads/{workspace}', { params: { path: { workspace }, query } })
  ensureOk(res, 'Failed to load mail')
  const d = res.data as MailThreadListOut
  return { items: [...d.items], unread_total: d.unread_total ?? 0, addresses: [...(d.addresses ?? [])],
    agent_address: d.agent_address ?? '', has_more: d.has_more ?? false }
}

type Outgoing = { text: string; to: string[]; cc: string[]; bcc: string[]; attachments: string[] }

/** Answer a thread from your own address; the reply joins the thread. */
export async function replyToThread(workspace: string, threadId: string, body: Outgoing): Promise<MailMessageOut> {
  const res = await apiV2.POST('/api/mail/threads/{workspace}/{uuid}/reply', {
    params: { path: { workspace, uuid: threadId } }, body: { html: '', ...body } })
  ensureOk(res, 'Failed to send the reply')
  return res.data as MailMessageOut
}

/** Start a new thread: as an agent (`agent`), or from one of your own forwarding addresses. */
export async function sendMail(workspace: string,
  body: Outgoing & { agent?: string; from_address: string; subject: string }): Promise<MailMessageOut> {
  const res = await apiV2.POST('/api/mail/send/{workspace}', { params: { path: { workspace } }, body: { html: '', ...body, agent: body.agent ?? '' } })
  ensureOk(res, 'Failed to send the message')
  return res.data as MailMessageOut
}

/** Delete a thread for good. Only from Trash or Spam. */
export async function deleteMailThread(workspace: string, threadId: string): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/threads/{workspace}/{uuid}', { params: { path: { workspace, uuid: threadId } } })
  ensureOk(res, 'Failed to delete the thread')
}

/** Delete everything in Trash or Spam for good. */
export async function emptyMailFolder(workspace: string, folder: 'trash' | 'spam', agent?: string): Promise<void> {
  const res = await apiV2.POST('/api/mail/empty-folder/{workspace}', {
    params: { path: { workspace }, query: { folder, agent: agent ?? '' } } })
  ensureOk(res, 'Failed to empty the folder')
}

/** Hold a picked file in storage; a send names it by id. */
export async function uploadMailAttachment(workspace: string, file: File): Promise<MailUploadOut> {
  const form = new FormData()
  form.append('file', file)
  const res = await apiV2.POST('/api/mail/uploads/{workspace}', {
    params: { path: { workspace } },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    body: form as any,
    bodySerializer: (body: unknown) => body as FormData,
  })
  ensureOk(res, 'Failed to attach the file')
  return res.data as MailUploadOut
}

export async function fetchMailDrafts(workspace: string, agent?: string): Promise<MailDraftOut[]> {
  const res = await apiV2.GET('/api/mail/drafts/{workspace}', { params: { path: { workspace }, query: agent ? { agent } : {} } })
  ensureOk(res, 'Failed to load drafts')
  return [...(res.data as { items: readonly MailDraftOut[] }).items]
}

export async function saveMailDraft(workspace: string, id: string, body: MailDraftIn): Promise<MailDraftOut> {
  const res = await apiV2.PUT('/api/mail/drafts/{workspace}/{draft_id}', { params: { path: { workspace, draft_id: id } }, body })
  ensureOk(res, 'Failed to save the draft')
  return res.data as MailDraftOut
}

export async function deleteMailDraft(workspace: string, id: string): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/drafts/{workspace}/{draft_id}', { params: { path: { workspace, draft_id: id } } })
  ensureOk(res, 'Failed to delete the draft')
}
