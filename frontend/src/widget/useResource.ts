import { useEffect, useRef } from 'react'

import { onResourceChanged } from './pageInvalidation'

/**
 * Re-read `resource` whenever canopy says it changed.
 *
 * ```tsx
 * useResource('task://', reload)   // AgentTasksSection
 * ```
 *
 * Pairs with `usePageState`: that one declares WHAT is on screen (including the
 * resource), this one says how to read it again. Declaring the resource without
 * this hook is legal and means "tell the agent what I show, but I will not
 * refresh myself" — which is a worse page, not a broken one.
 *
 * The ref keeps the subscription stable while letting the newest closure win, so
 * a caller does not have to memoise `refetch` and cannot accidentally
 * re-subscribe on every render.
 */
/** The resource canopy marks dirty whenever any task row moves
 *  (`apps/harness/signals.py::TASK_RESOURCE`) — every surface showing tasks
 *  (an agent's Tasks page, a project page, the fleet's Waiting on you) listens. */
export const TASK_RESOURCE = 'task://'

export function useResource(resource: string, refetch: () => void | Promise<void>): void {
  const latest = useRef(refetch)
  latest.current = refetch

  useEffect(() => onResourceChanged(resource, () => latest.current()), [resource])
}
