import type { AgentEdgeOut, AgentTopologyAgentOut } from '@/api/workspaces'

export function edgeKey(source: string, target: string): string {
  return `${source}\u0000${target}`
}

export function indexEdges(edges: readonly AgentEdgeOut[]): Map<string, AgentEdgeOut> {
  return new Map(edges.map((e) => [edgeKey(e.source, e.target), e] as const))
}

/** One sentence: why `src` gets what it gets from `dst`. */
export function explainEdge(e: AgentEdgeOut, src: AgentTopologyAgentOut, dst: AgentTopologyAgentOut): string {
  const login = src.login_email ?? src.name
  switch (e.basis) {
    case 'owner':
      return `${login} owns ${dst.name}, so ${src.name} gets all of ${dst.name}.`
    case 'workspace-owner':
      return `${login} owns ${dst.name}'s workspace, so ${src.name} gets all of ${dst.name}.`
    case 'admin':
      return `${login} is an admin of ${dst.name}, so ${src.name} gets all of ${dst.name}.`
    case 'full-rule':
      return `${dst.name}'s interface gives the whole agent to ${e.full_rule}, which includes ${login}.`
    case 'editor':
      return `${login} is an editor of ${dst.workspace}, so ${src.name} may send ${dst.name} work in its whole profile, but every such turn runs manual. Making it an admin of ${dst.name} lets it ask for auto.`
    case 'no-interface':
      return `${dst.name} has published no interface, so it takes work only from its admins and workspace editors; ${login} is a viewer there. Work ${src.name} sends is refused.`
    case 'capabilities':
      return `${dst.name}'s interface confines members to ${e.capabilities.join(', ')}. Work ${src.name} sends runs as that and nothing more, or is refused.`
    case 'nothing-offered':
      return `${login} is a member of ${dst.workspace}, but ${dst.name}'s interface offers members nothing. Work ${src.name} sends is refused.`
    case 'not-member':
      return `${login} is not a member of ${dst.workspace}, so ${src.name} cannot reach ${dst.name} at all. Add it to ${dst.workspace} first.`
    case 'no-login':
      return `${src.name} has no canopy login, so it cannot send other agents work directly. Link one on ${src.name} → Settings.`
    default:
      return e.basis
  }
}

/** Edges this viewer could fix with one admin grant: grantable and not already
 * full — or full only as an editor, which an admin grant lifts to auto. */
export function grantable(edges: readonly AgentEdgeOut[]): AgentEdgeOut[] {
  return edges.filter((e) => e.can_grant && (e.access !== 'full' || e.basis === 'editor'))
}
