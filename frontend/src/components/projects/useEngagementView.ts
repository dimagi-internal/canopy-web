import { useEffect } from 'react'

import { recordEngagementView } from '@/api/agents'

/**
 * Fire the view beacon once when a page about a project or a huddle opens —
 * what tells us whether anyone looks at a proposal at all (EngagementEvent,
 * `viewed`). The server counts one view per person, per object, per UTC day, so
 * a re-render or a reload costs nothing; a failure is ignored (telemetry never
 * breaks a page).
 */
export function useEngagementView(
  target: { project: number; workspace?: string } | { huddle_id: string; workspace?: string } | null,
): void {
  const project = target && 'project' in target ? target.project : undefined
  const huddle = target && 'huddle_id' in target ? target.huddle_id : undefined
  const workspace = target?.workspace
  useEffect(() => {
    if (project !== undefined) void recordEngagementView({ project, workspace }).catch(() => undefined)
    else if (huddle) void recordEngagementView({ huddle_id: huddle, workspace }).catch(() => undefined)
  }, [project, huddle, workspace])
}
