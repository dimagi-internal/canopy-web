// Read-only mail thread fetchers for the agent Mail section. Same shape as
// inbound.ts; may be merged into api/mail.ts later.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type MailThreadOut = components['schemas']['MailThreadOut']
export type MailMessageOut = components['schemas']['MailMessageOut']

export class MailThreadsApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export type MailThreadList = { items: MailThreadOut[]; agent_address: string }

export async function fetchMailThreads(workspace: string, agent?: string): Promise<MailThreadList> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}', {
    params: { path: { workspace }, query: agent ? { agent } : {} },
  })
  if (!res.response.ok) {
    throw new MailThreadsApiError(res.response.status, problemMessage(res.error, 'Failed to load mail threads'))
  }
  const d = res.data as { items: MailThreadOut[]; agent_address?: string }
  return { items: d.items, agent_address: d.agent_address ?? '' }
}

export async function fetchMailThread(workspace: string, threadId: string): Promise<MailThreadOut> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}/{uuid}', {
    params: { path: { workspace, uuid: threadId } },
  })
  if (!res.response.ok) {
    throw new MailThreadsApiError(res.response.status, problemMessage(res.error, 'Failed to load mail thread'))
  }
  return res.data as MailThreadOut
}
