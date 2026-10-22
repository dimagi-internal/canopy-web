import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type PresencePreferenceOut = components['schemas']['PresencePreferenceOut']

export async function getPresencePreference(): Promise<PresencePreferenceOut> {
  const res = await apiV2.GET('/api/me/presence-preference/')
  const data = unwrap(res, 'Failed to load presence preference')
  return data
}

export async function setPresencePreference(showPresence: boolean): Promise<PresencePreferenceOut> {
  const res = await apiV2.PATCH('/api/me/presence-preference/', {
    body: { show_presence: showPresence },
  })
  const data = unwrap(res, 'Failed to update presence preference')
  return data
}
