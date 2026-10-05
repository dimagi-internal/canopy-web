// A workspace's Slack connection, and canopy managing the app's slash commands.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type SlackConfigOut = components['schemas']['SlackConfigOut']
export type SlackSyncOut = components['schemas']['SlackSyncOut']
export type SlackDeclareAgentOut = components['schemas']['SlackDeclareAgentOut']

export async function getSlackConfig(workspace: string): Promise<SlackConfigOut> {
  const res = await apiV2.GET('/api/slack-config/{workspace}', { params: { path: { workspace } } })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load the Slack connection'))
  return res.data as SlackConfigOut
}

export async function setSlackConfigToken(workspace: string, refreshToken: string): Promise<SlackSyncOut> {
  const res = await apiV2.PUT('/api/slack-config/{workspace}/config-token', {
    params: { path: { workspace } },
    body: { refresh_token: refreshToken },
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Slack refused that token'))
  return res.data as SlackSyncOut
}

export async function clearSlackConfigToken(workspace: string): Promise<SlackConfigOut> {
  const res = await apiV2.DELETE('/api/slack-config/{workspace}/config-token', {
    params: { path: { workspace } },
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to disconnect'))
  return res.data as SlackConfigOut
}

// `@canopy <agent> --history <minutes> <ask>`: may an ask read the channel's
// recent past, and how far back. Owners only.
export async function setSlackHistory(
  workspace: string,
  enabled: boolean,
  maxMinutes: number,
): Promise<SlackConfigOut> {
  const res = await apiV2.PUT('/api/slack-config/{workspace}/history', {
    params: { path: { workspace } },
    body: { enabled, max_minutes: maxMinutes },
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to save the history setting'))
  return res.data as SlackConfigOut
}

export async function syncSlackCommands(workspace: string): Promise<SlackSyncOut> {
  const res = await apiV2.POST('/api/slack-config/{workspace}/sync', { params: { path: { workspace } } })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Sync failed'))
  return res.data as SlackSyncOut
}

export async function declareSlackAgent(workspace: string): Promise<SlackDeclareAgentOut> {
  const res = await apiV2.POST('/api/slack-config/{workspace}/declare-agent', {
    params: { path: { workspace } },
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Slack refused that change'))
  return res.data as SlackDeclareAgentOut
}

// "Added /hal." — one line saying what a sync did, for a banner.
export function syncSummary(r: SlackSyncOut): string {
  if (r.status !== 'synced') return r.detail || 'Not synced.'
  const parts = [
    r.added.length ? `Added ${r.added.join(', ')}` : '',
    r.updated?.length ? `Updated ${r.updated.join(', ')}` : '',   // absent from a pre-#1154 server
    r.removed.length ? `Removed ${r.removed.join(', ')}` : '',
    r.unfit.length ? `Too long for a Slack command: ${r.unfit.join(', ')}` : '',
    r.scopes_added.length
      ? `Added the ${r.scopes_added.join(', ')} permission — reconnect Slack to grant it` : '',
  ].filter(Boolean)
  return parts.length ? `${parts.join('. ')}.` : 'Slash commands already match.'
}
