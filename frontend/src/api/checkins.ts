// Project check-ins: a project's recurring meeting — a cadence, a lead, the
// agents that take part, optionally a Slack channel (apps/huddles/checkins.py).
// Nothing here reads a channel — a run is read only when its lead presses
// Confirm on the announcement in Slack; Run now only announces.
import { apiV2, WORKSPACE_HEADER } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type Checkin = components['schemas']['CheckinOut']
export type CheckinIn = components['schemas']['CheckinIn']
export type CheckinPatch = components['schemas']['CheckinPatch']
export type CheckinRun = components['schemas']['CheckinRunOut']
export type CheckinTimelineRun = components['schemas']['CheckinTimelineRunOut']

/** A project, addressed the way every check-in route does — pinned to its own workspace. */
export interface ProjectKey {
  agent: string
  ref: string
  workspace?: string
}

const pin = (workspace?: string) => (workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {})
const path = (p: ProjectKey) => ({ slug: p.agent, ref: p.ref })

export async function listProjectCheckins(p: ProjectKey): Promise<Checkin[]> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/{ref}/checkins/', {
    params: { path: path(p) },
    ...pin(p.workspace),
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load check-ins'))
  return Array.from(res.data ?? []) as unknown as Checkin[]
}

export async function createProjectCheckin(p: ProjectKey, body: CheckinIn): Promise<Checkin> {
  const res = await apiV2.POST('/api/agents/{slug}/projects/{ref}/checkins/', {
    params: { path: path(p) },
    ...pin(p.workspace),
    body,
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not add the check-in'))
  return res.data as unknown as Checkin
}

export async function updateProjectCheckin(p: ProjectKey, name: string, body: CheckinPatch): Promise<Checkin> {
  const res = await apiV2.PATCH('/api/agents/{slug}/projects/{ref}/checkins/{name}', {
    params: { path: { ...path(p), name } },
    ...pin(p.workspace),
    body,
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not save the check-in'))
  return res.data as unknown as Checkin
}

export async function deleteProjectCheckin(p: ProjectKey, name: string): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/projects/{ref}/checkins/{name}', {
    params: { path: { ...path(p), name } },
    ...pin(p.workspace),
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not delete the check-in'))
}

/** Announces it now (or, with no channel, queues the agent's turn) — never reads. */
export async function runProjectCheckinNow(p: ProjectKey, name: string): Promise<CheckinRun> {
  const res = await apiV2.POST('/api/agents/{slug}/projects/{ref}/checkins/{name}/run-now', {
    params: { path: { ...path(p), name } },
    ...pin(p.workspace),
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Could not run the check-in'))
  return res.data as unknown as CheckinRun
}

/** The project's timeline: every check-in run, newest first, with what it proposed. */
export async function listProjectCheckinRuns(p: ProjectKey): Promise<CheckinTimelineRun[]> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/{ref}/checkin-runs', {
    params: { path: path(p) },
    ...pin(p.workspace),
  })
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load the timeline'))
  return Array.from(res.data ?? []) as unknown as CheckinTimelineRun[]
}

/** Every check-in on a project in your workspaces — a navigation view. */
export async function listCheckins(): Promise<Checkin[]> {
  const res = await apiV2.GET('/api/checkins/')
  if (!res.response.ok) throw new Error(problemMessage(res.error, 'Failed to load check-ins'))
  return Array.from(res.data ?? []) as unknown as Checkin[]
}
