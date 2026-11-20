import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type Learning = components['schemas']['LearningOut']
export type LearningClass = components['schemas']['LearningClassOut']
export type DisclosureDay = components['schemas']['DisclosureDayOut']
export type RemovedSession = components['schemas']['RemovedSessionOut']
export type ReportingLine = components['schemas']['ReportingLineOut']

/** What you agreed canopy may learn from your sessions, by class. Only you see it. */
export async function getMyLearning(): Promise<Learning> {
  return unwrap(await apiV2.GET('/api/me/learning/'), 'Failed to load your learning settings') as Learning
}

export type LearningChange = components['schemas']['LearningIn']

/** Your act: the classes you list are the ones canopy may learn for. */
export async function setMyLearning(change: LearningChange): Promise<Learning> {
  const res = await apiV2.PUT('/api/me/learning/', { body: change })
  return unwrap(res, 'Failed to change your learning settings') as Learning
}

/** Stop all learning from your sessions, at once. */
export async function endMyLearning(): Promise<Learning> {
  return unwrap(await apiV2.DELETE('/api/me/learning/'), 'Failed to stop learning') as Learning
}

export async function listMyRemovedSessions(): Promise<RemovedSession[]> {
  return unwrap(await apiV2.GET('/api/me/learning/removed/'), 'Failed to load removed sessions') as RemovedSession[]
}

export async function getMyDisclosure(): Promise<DisclosureDay[]> {
  return unwrap(await apiV2.GET('/api/me/learning/disclosure/'), 'Failed to load what was read') as DisclosureDay[]
}

/** The manager lines you are in, in one workspace (you see only your own). */
export async function listReporting(slug: string): Promise<ReportingLine[]> {
  const res = await apiV2.GET('/api/brain/workspaces/{slug}/reporting/', { params: { path: { slug } } })
  return unwrap(res, 'Failed to load who manages whom') as ReportingLine[]
}
