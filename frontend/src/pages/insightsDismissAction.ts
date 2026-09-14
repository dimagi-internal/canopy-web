/**
 * "Close all the insights", as the agent actually calls it.
 *
 * Extracted from the component so it can be tested without rendering a page:
 * the two behaviours worth pinning are both about what the AGENT is told, and
 * neither is visible in the DOM.
 *
 * **Concurrent, not sequential.** A page action is refused after
 * `DEFAULT_TIMEOUT_SECONDS` (20s), and this is invoked on a list a user calls
 * stale — "a ton of stale stuff", which is the whole reason the feature exists.
 * One round trip per insight, awaited in turn, puts a large clear-out over that
 * budget, and the agent is then told the page is probably closed while the tab
 * is in fact still working through the list. Firing them together makes the
 * cost one round trip rather than N.
 *
 * **Partial failure is reported as partial.** Awaiting in a loop and letting
 * the first rejection escape left the worst possible state: the rows already
 * deleted server-side stayed on screen, because the throw jumped over the state
 * update — and the agent was told the whole thing failed. Settling every call
 * means the list matches the server, and the agent gets a count of what did and
 * did not happen instead of a single misleading verdict.
 */

export interface DismissDeps {
  /** Ids currently on the page. An id outside this set is refused. */
  visible: ReadonlySet<number>
  /** Delete one insight. Rejects on failure. */
  dismiss: (id: number) => Promise<unknown>
  /** Apply the successes to the page. Called even on partial failure. */
  onDismissed: (ids: number[]) => void
}

export interface DismissResult {
  dismissed: number
  ids: number[]
}

export async function dismissInsightsAction(
  ids: readonly number[],
  deps: DismissDeps,
): Promise<DismissResult> {
  // Only what is actually on screen. The agent was handed this list, so an id
  // outside it means the page moved on — dismissing it anyway would act on
  // something the user is no longer looking at.
  const unknown = ids.filter((id) => !deps.visible.has(id))
  if (unknown.length) {
    throw new Error(
      `not on this page: ${unknown.join(', ')}. The list may have changed since you read it.`,
    )
  }

  const outcomes = await Promise.allSettled(ids.map((id) => deps.dismiss(id)))
  const done: number[] = []
  const failed: number[] = []
  outcomes.forEach((outcome, i) => {
    ;(outcome.status === 'fulfilled' ? done : failed).push(ids[i])
  })

  // Before the throw, always: these rows are gone from the server, so leaving
  // them on screen would make the page disagree with it.
  if (done.length) deps.onDismissed(done)

  if (failed.length) {
    throw new Error(
      `dismissed ${done.length} of ${ids.length}; ${failed.join(', ')} could not be dismissed`,
    )
  }
  return { dismissed: done.length, ids: done }
}
