/**
 * The page's side of the action registry.
 *
 * A module-level registry for the same reason `pageContext` uses one: the
 * consumer is not a React component. `CanopyWidget` hands the widget plain
 * callbacks and the widget mounts its own DOM outside the tree, so a provider
 * read from inside the tree and mirrored out would be a second copy of the
 * same state.
 *
 * Actions are page-scoped and ephemeral by nature. They exist while a page is
 * mounted and vanish with it, which is why the agent-facing contract refuses
 * rather than queues (apps/canopy_sessions/page_actions.py).
 */

export interface PageActionSpec {
  name: string
  description?: string
  /** JSON-Schema for the arguments object. Without it the agent knows the
   *  action exists but not how to call it. */
  parameters?: Record<string, unknown>
}

type Runner = (args: Record<string, unknown>) => unknown | Promise<unknown>

const registry = new Map<string, { spec: PageActionSpec; run: Runner }>()
const listeners = new Set<(specs: PageActionSpec[]) => void>()

function notify(): void {
  const specs = currentSpecs()
  listeners.forEach((l) => l(specs))
}

/** Sorted, so a page registering in a different order across renders does not
 *  hand the agent a different-looking tool list each time. */
export function currentSpecs(): PageActionSpec[] {
  return [...registry.values()]
    .map((e) => e.spec)
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** Register one action. Returns a disposer; last registration wins. */
export function registerPageAction(spec: PageActionSpec, run: Runner): () => void {
  registry.set(spec.name, { spec, run })
  notify()
  return () => {
    // Only if it is still OURS: React can fire an old component's cleanup
    // after a new one registered the same name, and deleting unconditionally
    // would withdraw a live action.
    if (registry.get(spec.name)?.run === run) {
      registry.delete(spec.name)
      notify()
    }
  }
}

/** Subscribe to changes, so the widget can re-declare as the page moves. */
export function onPageActionsChanged(fn: (specs: PageActionSpec[]) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export class UnknownPageActionError extends Error {
  readonly actionName: string
  constructor(name: string) {
    super(`no page action named ${JSON.stringify(name)} is registered here`)
    this.actionName = name
  }
}

/** Run one. Rejects for an unknown name rather than doing nothing — a silent
 *  no-op is indistinguishable to the agent from success. */
export async function runPageAction(
  name: string,
  args: Record<string, unknown>,
): Promise<unknown> {
  const entry = registry.get(name)
  if (!entry) throw new UnknownPageActionError(name)
  return entry.run(args)
}
