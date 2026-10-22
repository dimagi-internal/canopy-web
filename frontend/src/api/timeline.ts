/**
 * API client for the /api/timeline surface (team-wide activity aggregator).
 * On `apiV2`, so a tenant page's read is pinned to /api/w/<ws>/timeline/.
 */

import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

type Schemas = components['schemas']

export type TimelineEvent = Schemas['ActivityEventOut']
export type TimelineSubsystem = Schemas['SubsystemOut']
export type TimelineResponse = Schemas['TimelineOut']

export interface ListTimelineParams {
  subsystem?: string
  limit?: number
  before?: string
}

export async function listTimeline(params: ListTimelineParams = {}): Promise<TimelineResponse> {
  const res = await apiV2.GET('/api/timeline/', {
    params: {
      query: {
        ...(params.subsystem ? { subsystem: params.subsystem } : {}),
        ...(params.limit ? { limit: params.limit } : {}),
        ...(params.before ? { before: params.before } : {}),
      },
    },
  })
  return unwrap(res, 'listTimeline') as unknown as TimelineResponse
}
