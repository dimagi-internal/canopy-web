import { useEffect, useRef } from 'react'

import { notifyPageStateChanged, setPageStateContributor, type PageState } from './pageState'

/**
 * Declare what this page is currently showing.
 *
 * ```tsx
 * usePageState(
 *   () => describeSelection({
 *     backingTool: 'list_insights',
 *     ids: insights.map((i) => i.id),
 *     filters: { category: activeFilter },
 *   }),
 *   [insights, activeFilter],
 * )
 * ```
 *
 * **The deps array is required, and it is the whole difference from
 * `usePageContext`.** That hook read its callback once, when a conversation
 * opened, so an empty deps array was correct: re-registering bought nothing.
 * This one PUSHES — a change in the view is sent to the agent as it happens —
 * so the deps are what tell it the view changed. Omitting a dependency here
 * does not cause a stale render; it causes the agent to act on a screen that
 * has moved, which is the failure the state channel exists to remove.
 *
 * Registering is last-wins and unregisters on unmount, so navigating away
 * cannot leave the agent describing a page you have left.
 */
export function usePageState(build: () => PageState, deps: unknown[]): void {
  // The ref keeps the registration stable while letting the newest closure win,
  // so re-running on deps costs a notify rather than a re-register.
  const latest = useRef(build)
  latest.current = build

  useEffect(() => setPageStateContributor(() => latest.current()), [])

  useEffect(() => {
    notifyPageStateChanged()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}
