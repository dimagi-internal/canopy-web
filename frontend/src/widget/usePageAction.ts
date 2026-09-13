import { useEffect, useRef } from 'react'

import { registerPageAction, type PageActionSpec } from './pageActions'

/**
 * Offer the embedded agent something this page can do.
 *
 * ```tsx
 * usePageAction('dismissInsights', async ({ ids }) => { … }, {
 *   description: 'Dismiss insights from the list the user is viewing',
 *   parameters: { type: 'object', properties: { ids: { type: 'array' } }, required: ['ids'] },
 * })
 * ```
 *
 * **Throw to refuse.** A thrown error reaches the agent as a refusal it can
 * read and act on; returning something falsy reads as success, and the agent
 * carries on as though the page changed.
 *
 * The callback is read at CALL time, so it may close over current state freely
 * and does not need memoising — the deps array is empty on purpose, and the
 * ref is what makes that correct. Registration is last-wins and unregisters on
 * unmount, so navigating away cannot leave the agent able to call into a page
 * that is gone.
 */
export function usePageAction(
  name: string,
  run: (args: Record<string, unknown>) => unknown | Promise<unknown>,
  options?: Omit<PageActionSpec, 'name'>,
): void {
  const latest = useRef(run)
  latest.current = run

  // The spec is serialised and sent to canopy, so it must not change identity
  // every render — stringify is the cheap stable comparison for a plain
  // JSON-Schema object.
  const specKey = JSON.stringify({ name, ...options })

  useEffect(
    () =>
      registerPageAction(
        { name, description: options?.description, parameters: options?.parameters },
        (args) => latest.current(args),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [specKey],
  )
}
