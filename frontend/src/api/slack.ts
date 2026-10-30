// A workspace's Slack connection, and canopy managing the app's slash commands.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'

export type SlackConfigOut = components['schemas']['SlackConfigOut']
export type SlackSyncOut = components['schemas']['SlackSyncOut']

export async function getSlackConfig(workspace: string): Promise<SlackConfigOut> {
  const res = await apiV2.GET('/api/slack-config/{workspace}', { params: { path: { workspace } } })
  ensureOk(res, 'Failed to load the Slack connection')
  return res.data as SlackConfigOut
}

export async function setSlackConfigToken(workspace: string, refreshToken: string): Promise<SlackSyncOut> {
  const res = await apiV2.PUT('/api/slack-config/{workspace}/config-token', {
    params: { path: { workspace } },
    body: { refresh_token: refreshToken },
  })
  ensureOk(res, 'Slack refused that token')
  return res.data as SlackSyncOut
}

export async function clearSlackConfigToken(workspace: string): Promise<SlackConfigOut> {
  const res = await apiV2.DELETE('/api/slack-config/{workspace}/config-token', {
    params: { path: { workspace } },
  })
  ensureOk(res, 'Failed to disconnect')
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
  ensureOk(res, 'Failed to save the history setting')
  return res.data as SlackConfigOut
}

// Whether this workspace's agents may ASK to read a whole channel (each read
// still needs an approval posted in the channel). Owners only.
export async function setSlackChannelIngest(workspace: string, enabled: boolean): Promise<SlackConfigOut> {
  const res = await apiV2.PUT('/api/slack-config/{workspace}/channel-ingest', {
    params: { path: { workspace } },
    body: { enabled },
  })
  ensureOk(res, 'Failed to save the channel-read setting')
  return res.data as SlackConfigOut
}

// An admin's explicit allow-list: one channel (id or #name) for one agent. A
// matching ask reads at once, with a notice in the channel. Admins and owners.
export async function allowSlackChannel(
  workspace: string,
  body: { agent: string; channel: string; follow: boolean; note: string; review_sessions?: boolean },
): Promise<SlackConfigOut> {
  const res = await apiV2.POST('/api/slack-config/{workspace}/channel-ingest/allowed', {
    params: { path: { workspace } },
    body,
  })
  ensureOk(res, 'Failed to allow-list that channel')
  return res.data as SlackConfigOut
}

// Removing an entry also revokes the reads it allowed. Admins and owners.
export async function disallowSlackChannel(workspace: string, allowId: string): Promise<SlackConfigOut> {
  const res = await apiV2.DELETE('/api/slack-config/{workspace}/channel-ingest/allowed/{allow_id}', {
    params: { path: { workspace, allow_id: allowId } },
  })
  ensureOk(res, 'Failed to remove that channel')
  return res.data as SlackConfigOut
}

export async function syncSlackCommands(workspace: string): Promise<SlackSyncOut> {
  const res = await apiV2.POST('/api/slack-config/{workspace}/sync', { params: { path: { workspace } } })
  ensureOk(res, 'Sync failed')
  return res.data as SlackSyncOut
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
    r.webhooks_moved?.length ? `Moved the ${r.webhooks_moved.join(' and ')} URL to this address` : '',
    r.redirects_removed?.length ? `Removed the sign-in callback on a former address` : '',
    r.declared?.length ? `Turned on Slack's own "Working…" indicator` : '',
  ].filter(Boolean)
  return parts.length ? `${parts.join('. ')}.` : 'Slash commands already match.'
}
