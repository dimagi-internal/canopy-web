/**
 * "The data you are showing changed — read it again."
 *
 * The receiving end of `apps/canopy_sessions/invalidation.py`. A page registers
 * how to refetch itself; the widget calls it when canopy says the resource it
 * declared has moved.
 *
 * **Refetch, not patch.** The notification carries a resource URI and nothing
 * else — MCP's `notifications/resources/updated` shape — so the page re-reads
 * through the path it already uses, where its own authorization applies. A diff
 * would be a second source of truth for data the page already knows how to
 * load, and would have to be kept correct forever.
 *
 * **Why the page decides, not the widget.** Only the page knows what "re-read"
 * means for it: which query, which filters, which loading state to show. The
 * widget knows only that something moved.
 *
 * A module-level registry for the same reason `pageActions` and `pageState` use
 * one: the consumer is not a React component.
 */

type Refetch = () => void | Promise<void>

const handlers = new Map<string, Set<Refetch>>()

/** Register how to re-read `resource`. Returns a disposer. */
export function onResourceChanged(resource: string, refetch: Refetch): () => void {
  const set = handlers.get(resource) ?? new Set()
  set.add(refetch)
  handlers.set(resource, set)
  return () => {
    set.delete(refetch)
    if (set.size === 0) handlers.delete(resource)
  }
}

/**
 * Tell whoever is showing `resource` to re-read. Returns how many were told.
 *
 * Never throws: a refetch that fails is a page showing slightly old data, while
 * an exception escaping here would propagate into the socket handler and could
 * take down the connection carrying the conversation.
 */
export function resourceChanged(resource: string): number {
  const set = handlers.get(resource)
  if (!set) return 0
  for (const refetch of set) {
    try {
      const result = refetch()
      if (result && typeof (result as Promise<void>).catch === 'function') {
        void (result as Promise<void>).catch(() => undefined)
      }
    } catch {
      // See above.
    }
  }
  return set.size
}

/** Test seam. Not used in the app — a leaked handler between tests is a
 *  cross-test dependency, not a product behaviour. */
export function resetResourceHandlers(): void {
  handlers.clear()
}
