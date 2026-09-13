import { useEffect, useRef } from 'react'

import { setPageContributor } from './pageContext'

/**
 * Contribute what is on screen to the agent's view of this page.
 *
 * ```tsx
 * usePageContext(() => ({
 *   openItems: items.map((i) => ({ id: i.id, kind: i.kind, age_days: i.ageDays })),
 * }))
 * ```
 *
 * Read at the moment a conversation opens, not on every render, so the
 * callback may close over current state freely and does not need to be
 * memoised. The deps array is deliberately empty: re-registering on every
 * render would be churn for no gain, since the registry only ever calls the
 * latest callback.
 *
 * Registering is last-wins and unregisters on unmount, so navigating away
 * cannot leave the agent describing a page you have left.
 */
export function usePageContext(build: () => Record<string, unknown>): void {
  // The ref is what makes the empty deps array correct: the registry holds a
  // stable wrapper, and the wrapper always calls the newest closure.
  const latest = useRef(build)
  latest.current = build

  useEffect(() => setPageContributor(() => latest.current()), [])
}
