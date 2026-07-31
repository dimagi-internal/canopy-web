// Inbound push API — per-workspace Gmail push config and the mailboxes it
// covers. This is the surface that replaced env vars + a Django shell, which is
// what makes the app multi-tenant: a second workspace configures its own
// audience, signer and topic without touching the deployment.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type PushConfigOut = components['schemas']['PushConfigOut']
export type MailboxOut = components['schemas']['MailboxOut']
export type WatchState = MailboxOut['watch_state']

// Mirrors WorkspaceApiError: callers need the status (403 for a non-owner write,
// 404 for a non-member, 409 for an address already registered) to branch, not
// just a message.
export class InboundApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function getPushConfig(workspace: string): Promise<PushConfigOut> {
  const res = await apiV2.GET('/api/inbound/config/{workspace}', {
    params: { path: { workspace } },
  })
  if (!res.response.ok) {
    throw new InboundApiError(res.response.status, problemMessage(res.error, 'Failed to load push config'))
  }
  return res.data as PushConfigOut
}

export async function setPushConfig(
  workspace: string,
  body: { audience: string; service_account: string; watch_topic: string },
): Promise<PushConfigOut> {
  const res = await apiV2.PUT('/api/inbound/config/{workspace}', {
    params: { path: { workspace } },
    body,
  })
  if (!res.response.ok) {
    throw new InboundApiError(res.response.status, problemMessage(res.error, 'Failed to save push config'))
  }
  return res.data as PushConfigOut
}

export async function listMailboxes(workspace: string): Promise<MailboxOut[]> {
  const res = await apiV2.GET('/api/inbound/mailboxes/{workspace}', {
    params: { path: { workspace } },
  })
  if (!res.response.ok) {
    throw new InboundApiError(res.response.status, problemMessage(res.error, 'Failed to load mailboxes'))
  }
  return (res.data as { items: MailboxOut[] }).items
}

export async function createMailbox(
  workspace: string,
  body: { address: string; agent_slug: string },
): Promise<MailboxOut> {
  const res = await apiV2.POST('/api/inbound/mailboxes/{workspace}', {
    params: { path: { workspace } },
    body: { ...body, enabled: true },
  })
  if (!res.response.ok) {
    throw new InboundApiError(res.response.status, problemMessage(res.error, 'Failed to add mailbox'))
  }
  return res.data as MailboxOut
}

export async function setMailboxEnabled(
  workspace: string,
  mailboxId: number,
  enabled: boolean,
): Promise<MailboxOut> {
  const res = await apiV2.PATCH('/api/inbound/mailboxes/{workspace}/{mailbox_id}', {
    params: { path: { workspace, mailbox_id: mailboxId } },
    body: { enabled },
  })
  if (!res.response.ok) {
    throw new InboundApiError(res.response.status, problemMessage(res.error, 'Failed to update mailbox'))
  }
  return res.data as MailboxOut
}

export async function deleteMailbox(workspace: string, mailboxId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/inbound/mailboxes/{workspace}/{mailbox_id}', {
    params: { path: { workspace, mailbox_id: mailboxId } },
  })
  if (!res.response.ok) {
    throw new InboundApiError(res.response.status, problemMessage(res.error, 'Failed to remove mailbox'))
  }
}
