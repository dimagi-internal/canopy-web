/**
 * Which workspace the host page is in, from what the page tells the panel.
 *
 * canopy's own panel offers the agents of the workspace on screen (the site's
 * own list in the site's own workspace; `apps/embedding/embed_api.py`). The
 * frame is created once and outlives navigation, so a list loaded on a dimagi
 * page would otherwise keep offering dimagi's agents on a family page. The page
 * state the host pushes carries its route (`widget/pageContext.ts`), and that is
 * enough to notice the move.
 */
export function workspaceOfPage(state: Record<string, unknown> | null | undefined): string {
  const path = state && typeof state.path === 'string' ? state.path : ''
  return /^\/w\/([^/?#]+)/.exec(path)?.[1] ?? ''
}

/** Only a panel with no conversation on screen swaps its list: one in progress stays. */
export function mayReloadAgents(phaseKind: string): boolean {
  return phaseKind === 'ready' || phaseKind === 'choosing'
}
