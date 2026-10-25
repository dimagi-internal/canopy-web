/**
 * Which agent the panel opens on, or null for the picker.
 *
 * canopy ranks the first three and marks the winner `default` on the agent list
 * (`apps/embedding/defaults.py`): the agent the page chose (its token is bound,
 * so it is the only one listed), else the visitor's own preference, else the
 * site's default. Below that the panel decides for itself, from facts about the
 * page: the agent the host named at `canopy.init` — for an SDK host that is its
 * `AGENT_SLUG` fallback, not a choice, since a page's real choice binds the
 * token — then the only agent on offer, then the picker.
 */
export interface PickableAgent {
  slug: string
  default?: boolean
}

export function pickInitialAgent<A extends PickableAgent>(agents: A[], hostAgent?: string): A | null {
  return (
    agents.find((a) => a.default) ??
    (hostAgent ? agents.find((a) => a.slug === hostAgent) : undefined) ??
    (agents.length === 1 ? agents[0] : null)
  )
}
