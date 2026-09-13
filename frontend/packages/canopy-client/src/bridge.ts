/**
 * The host bridge contract: how a product lends an agent its page.
 *
 * Defined here, at the framework-free layer, because the v2 spec (§8) requires
 * ONE definition with two transports. A React host mounting `canopy-ui/chat`
 * calls these functions directly; the iframe widget reaches the identical
 * functions over `postMessage`. If the contract lived in the widget, a native
 * host would have to reimplement it and the two would drift — which is the
 * mistake `RunnerAssignment` and `runner_tenant_slugs` were both cleanups of.
 *
 * **Why a snapshot and not a live feed.** `provideContext` is pulled when a
 * session opens, not subscribed to. A continuously-synced readable channel
 * (CopilotKit's `useCopilotReadable` is the reference shape) is real future
 * work; it is not needed to prove that an agent can reason about the page, and
 * every tick of it is a message the host has to be trusted to get right.
 *
 * **What the ACL story is.** The host resolves context and runs actions *in the
 * user's own session*, so an agent sees and does exactly what that user could —
 * never more. That is why v1 takes this route rather than giving the agent its
 * own credential for the host (v2 spec §8, option (b)): there is nothing here
 * that can exceed the user, so there is nothing to leak.
 *
 * **The limit that follows.** Both halves need the page to be open. Close the
 * tab and the agent cannot read or act; there is no server-side path. Action is
 * scoped to the life of the visit, and a host should not describe it otherwise.
 */

/** Arbitrary JSON the host chooses to expose. Deliberately opaque: canopy never
 *  interprets it, the same way `Session.metadata` is an opaque bag. */
export type HostContext = Record<string, unknown>

/** Pulled when a session opens. Synchronous or not — a host may need to read
 *  something async, and the caller always awaits. */
export type ContextProvider = () => HostContext | Promise<HostContext>

/**
 * One thing an agent may ask the page to do.
 *
 * Returning a value is how the agent learns whether it worked; THROWING is how
 * a host refuses. A refusal must be an error and not a `false`, because a
 * silently-ignored action is indistinguishable to the agent from a successful
 * one, and it will carry on as though the change landed.
 */
export type HostAction = (args: Record<string, unknown>) => unknown | Promise<unknown>

export interface HostBridge {
  /** Register what the agent may read. Replaces any previous provider — a page
   *  has one current state, not an accumulating list of them. */
  provideContext(provider: ContextProvider): void
  /** Register one named action. Re-registering a name replaces it, so a
   *  re-rendering host component can call this freely without stacking
   *  handlers. */
  registerAction(name: string, action: HostAction): void
  /** Stop offering an action — e.g. the user navigated away from the thing it
   *  acted on, and running it now would mutate something off-screen. */
  unregisterAction(name: string): void
  /** Read the current snapshot. `{}` when the host registered no provider,
   *  rather than throwing: a host that lends no context is a legitimate host. */
  readContext(): Promise<HostContext>
  /** Names currently offered, so the agent can be told what it may call. */
  actionNames(): string[]
  /** Run one. Rejects with `UnknownActionError` if it was never registered or
   *  has since been withdrawn — never a silent no-op. */
  runAction(name: string, args?: Record<string, unknown>): Promise<unknown>
}

export class UnknownActionError extends Error {
  constructor(readonly name: string) {
    super(
      `no host action named ${JSON.stringify(name)} is registered. ` +
        'It may have been withdrawn because the page moved on.',
    )
  }
}

export function createHostBridge(): HostBridge {
  let provider: ContextProvider | null = null
  const actions = new Map<string, HostAction>()

  return {
    provideContext(next) {
      provider = next
    },
    registerAction(name, action) {
      actions.set(name, action)
    },
    unregisterAction(name) {
      actions.delete(name)
    },
    async readContext() {
      if (!provider) return {}
      return provider()
    },
    actionNames() {
      // Sorted so a host that registers in a different order across renders
      // does not produce a different tool list for the agent each time.
      return [...actions.keys()].sort()
    },
    async runAction(name, args = {}) {
      const action = actions.get(name)
      if (!action) throw new UnknownActionError(name)
      return action(args)
    },
  }
}
