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

// ---- one session ------------------------------------------------------------------------------

export type SessionLearning = components['schemas']['LearningSourceOut']
export type SessionProjectLink = components['schemas']['SourceLinkOut']

const sessionPath = (sessionId: string) => ({ params: { path: { session_id: sessionId } } })

/** Whether this session is out of learning, and the projects it is tied to. 404 unless you are in it. */
export async function getMySessionLearning(sessionId: string, source = 'live'): Promise<SessionLearning> {
  const res = await apiV2.GET('/api/me/learning/sessions/{session_id}/', {
    params: { path: { session_id: sessionId }, query: { source } },
  })
  return unwrap(res, 'Failed to load this session’s learning') as SessionLearning
}

/** Your act: nothing may learn from this session again; what was derived from it is withdrawn. */
export async function removeMySession(sessionId: string): Promise<SessionLearning> {
  const res = await apiV2.PUT('/api/me/learning/sessions/{session_id}/', sessionPath(sessionId))
  return unwrap(res, 'Failed to remove this session from learning') as SessionLearning
}

/** Undo a removal. What was already withdrawn stays withdrawn. */
export async function restoreMySession(sessionId: string): Promise<SessionLearning> {
  const res = await apiV2.DELETE('/api/me/learning/sessions/{session_id}/', sessionPath(sessionId))
  return unwrap(res, 'Failed to let learning use this session again') as SessionLearning
}

/** Your call on whether the session belongs to a project: `core` it does, `rejected` it does not. */
export async function declareMySessionProject(
  sessionId: string,
  handle: string,
  tier: 'core' | 'rejected',
): Promise<SessionLearning> {
  const res = await apiV2.PUT('/api/me/learning/sessions/{session_id}/projects/{handle}/', {
    params: { path: { session_id: sessionId, handle } },
    body: { tier },
  })
  return unwrap(res, 'Failed to change the project link') as SessionLearning
}

/** Take back your own call; canopy's automatic guess resumes. */
export async function undeclareMySessionProject(sessionId: string, handle: string): Promise<SessionLearning> {
  const res = await apiV2.DELETE('/api/me/learning/sessions/{session_id}/projects/{handle}/', {
    params: { path: { session_id: sessionId, handle } },
  })
  return unwrap(res, 'Failed to take back your call') as SessionLearning
}
