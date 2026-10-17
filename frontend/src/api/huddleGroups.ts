// Work groups: a lead, a Slack channel, agents and a cadence (apps/huddles/groups.py).
// Nothing here reads a channel — a run is read only when its lead presses
// Confirm on the announcement in Slack.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type HuddleGroup = components['schemas']['WorkGroupOut']
export type HuddleGroupIn = components['schemas']['WorkGroupIn']
export type HuddleGroupPatch = components['schemas']['WorkGroupPatch']
export type HuddleGroupRun = components['schemas']['WorkGroupRunOut']

export async function listHuddleGroups(): Promise<HuddleGroup[]> {
  const res = await apiV2.GET('/api/huddle-groups/')
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load work groups'))
  return Array.from(res.data ?? []) as unknown as HuddleGroup[]
}

export async function getHuddleGroup(slug: string): Promise<HuddleGroup> {
  const res = await apiV2.GET('/api/huddle-groups/{slug}', { params: { path: { slug } } })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load the work group'))
  return res.data as unknown as HuddleGroup
}

export async function createHuddleGroup(body: HuddleGroupIn): Promise<HuddleGroup> {
  const res = await apiV2.POST('/api/huddle-groups/', { body })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not create the work group'))
  return res.data as unknown as HuddleGroup
}

export async function updateHuddleGroup(slug: string, body: HuddleGroupPatch): Promise<HuddleGroup> {
  const res = await apiV2.PATCH('/api/huddle-groups/{slug}', { params: { path: { slug } }, body })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not save the work group'))
  return res.data as unknown as HuddleGroup
}

export async function deleteHuddleGroup(slug: string): Promise<void> {
  const res = await apiV2.DELETE('/api/huddle-groups/{slug}', { params: { path: { slug } } })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not delete the work group'))
}

export async function listHuddleGroupRuns(slug: string): Promise<HuddleGroupRun[]> {
  const res = await apiV2.GET('/api/huddle-groups/{slug}/runs', { params: { path: { slug } } })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load runs'))
  return Array.from(res.data ?? []) as unknown as HuddleGroupRun[]
}
