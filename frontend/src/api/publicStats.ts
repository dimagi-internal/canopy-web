/**
 * Aggregate counts for the public explainer. Anonymous: the page is served to
 * visitors with no session, so there is nothing to send and a failure is silent.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'

export type PublicStats = components['schemas']['PublicStatsOut']

export async function getPublicStats(): Promise<PublicStats | null> {
  try {
    const res = await apiV2.GET('/api/system/public-stats')
    return res.response.ok && res.data ? res.data : null
  } catch {
    // The page must render without numbers rather than fail — a visitor who
    // cannot reach the API should still learn what canopy is.
    return null
  }
}
