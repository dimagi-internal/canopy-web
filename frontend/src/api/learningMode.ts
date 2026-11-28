import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type LearningMode = components['schemas']['LearningModeOut']
export type MemberLearningMode = components['schemas']['MemberLearningModeOut']
export type MemberLearningModeChange = components['schemas']['MemberLearningModeChangeOut']

/** Your learning mode: on or off, who turned it on and when, and exactly who may read your sessions. */
export async function getMyLearningMode(): Promise<LearningMode> {
  return unwrap(await apiV2.GET('/api/people/me/learning-mode/'), 'Failed to load your learning mode') as LearningMode
}

/** Your act: turn learning mode on or off. */
export async function setMyLearningMode(on: boolean): Promise<LearningMode> {
  const res = await apiV2.PUT('/api/people/me/learning-mode/', { body: { on } })
  return unwrap(res, 'Failed to change your learning mode') as LearningMode
}

/** Owners: every person in this workspace or below, and whether they are in learning mode here. */
export async function listMembersLearningMode(slug: string): Promise<MemberLearningMode[]> {
  const res = await apiV2.GET('/api/coaching/workspaces/{slug}/learning-mode/', { params: { path: { slug } } })
  return unwrap(res, 'Failed to load learning mode') as MemberLearningMode[]
}

/** Owners: turn a member's learning mode on or off, in the workspaces of theirs you own. */
export async function setMemberLearningMode(slug: string, userId: number, on: boolean): Promise<MemberLearningModeChange> {
  const res = await apiV2.PUT('/api/coaching/workspaces/{slug}/learning-mode/{user_id}/', {
    params: { path: { slug, user_id: userId } },
    body: { on },
  })
  return unwrap(res, 'Failed to change their learning mode') as MemberLearningModeChange
}
