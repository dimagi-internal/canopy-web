import { apiV2 } from './client.v2'
import type { components } from './generated'

export type PresencePreferenceOut = components['schemas']['PresencePreferenceOut']

export async function getPresencePreference(): Promise<PresencePreferenceOut> {
  const { data, error } = await apiV2.GET('/api/me/presence-preference/')
  if (error) throw new Error('Failed to load presence preference')
  return data
}

export async function setPresencePreference(showPresence: boolean): Promise<PresencePreferenceOut> {
  const { data, error } = await apiV2.PATCH('/api/me/presence-preference/', {
    body: { show_presence: showPresence },
  })
  if (error) throw new Error('Failed to update presence preference')
  return data
}
