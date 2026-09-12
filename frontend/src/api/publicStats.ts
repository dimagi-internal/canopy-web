/**
 * Aggregate counts for the public explainer. Anonymous — deliberately a bare
 * fetch with no credentials and no CSRF, because this page is served to
 * visitors with no session and sending cookies would gain nothing.
 */
import { apiUrl } from './base'

export interface PublicStats {
  agents: number
  skills: number
  runners_online: number
  turns_executed: number
  demos_published: number
}

export async function getPublicStats(): Promise<PublicStats | null> {
  try {
    const resp = await fetch(apiUrl('/api/system/public-stats'))
    if (!resp.ok) return null
    return (await resp.json()) as PublicStats
  } catch {
    // The page must render without numbers rather than fail — a visitor who
    // cannot reach the API should still learn what canopy is.
    return null
  }
}
