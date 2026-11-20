import type { GraphEdgeOut, GraphNodeOut, WorkspaceGraphOut } from '@/api/workspaces'
import { scopedPath } from '@/lib/scopedLinks'

// The pure half of the relations view (`GET /api/workspaces/{slug}/graph`): how each
// type of node and kind of edge is drawn, what the filters keep, a node's
// neighbourhood, search, and where a node's own page is. The 3-D canvas lives in
// RelationsCanvas3D (lazy, with three.js); the panel in RelationsView.

export type NodeType = GraphNodeOut['type']
export type EdgeKind = GraphEdgeOut['kind']
export type Shape = 'sphere' | 'octahedron' | 'box' | 'dodecahedron' | 'tetrahedron' | 'torus' | 'cone'

export const NODE_TYPES: readonly NodeType[] = ['person', 'agent', 'session', 'project', 'task', 'workspace', 'output']
export const EDGE_KINDS: readonly EdgeKind[] = [
  'participates', 'started', 'ran', 'dispatched_from', 'linked', 'reports_to', 'coaches', 'derived_from', 'about', 'within',
]
export const TIERS = ['core', 'adjacent', 'candidate', 'rejected'] as const
export type Tier = typeof TIERS[number]

/** Colour (a design token) and shape for each type of node. */
export const NODE_STYLE: Record<NodeType, { label: string; token: string; shape: Shape; size: number }> = {
  person: { label: 'People', token: '--success', shape: 'sphere', size: 4 },
  agent: { label: 'Agents', token: '--special', shape: 'octahedron', size: 5 },
  session: { label: 'Sessions', token: '--info', shape: 'box', size: 2.5 },
  project: { label: 'Projects', token: '--warning', shape: 'dodecahedron', size: 6 },
  task: { label: 'Tasks', token: '--chart-1', shape: 'tetrahedron', size: 3 },
  workspace: { label: 'Workspaces', token: '--muted-foreground', shape: 'torus', size: 8 },
  output: { label: 'Learning outputs', token: '--destructive', shape: 'cone', size: 3 },
}

/** Colour, width and words for each kind of edge. */
export const EDGE_STYLE: Record<EdgeKind, { label: string; token: string; width: number }> = {
  participates: { label: 'In a session', token: '--success', width: 0.4 },
  started: { label: 'Started a session', token: '--success', width: 0.9 },
  ran: { label: 'Agent ran a session', token: '--special', width: 0.6 },
  dispatched_from: { label: 'Dispatched from a task', token: '--chart-1', width: 0.9 },
  linked: { label: 'Session tied to a project', token: '--warning', width: 1 },
  reports_to: { label: 'Reports to', token: '--foreground', width: 1.2 },
  coaches: { label: 'Coaches', token: '--primary', width: 1 },
  derived_from: { label: 'Learned from', token: '--destructive', width: 0.6 },
  about: { label: 'Output about', token: '--destructive', width: 0.4 },
  within: { label: 'Belongs to', token: '--muted-foreground', width: 0.2 },
}

/** A project link's width and opacity by tier: core is the strongest line, a candidate a faint one. */
export const TIER_STYLE: Record<Tier, { label: string; width: number; opacity: number; token?: string }> = {
  core: { label: 'Core', width: 1.6, opacity: 0.9 },
  adjacent: { label: 'Adjacent', width: 0.8, opacity: 0.7 },
  candidate: { label: 'Candidate', width: 0.3, opacity: 0.45 },
  rejected: { label: 'Rejected (your call)', width: 0.3, opacity: 0.6, token: '--destructive' },
}

export function edgeToken(e: GraphEdgeOut): string {
  return (e.kind === 'linked' && TIER_STYLE[e.tier as Tier]?.token) || EDGE_STYLE[e.kind].token
}

export function edgeWidth(e: GraphEdgeOut): number {
  if (e.kind === 'linked') return TIER_STYLE[e.tier as Tier]?.width ?? 0.5
  return EDGE_STYLE[e.kind].width
}

export function edgeOpacity(e: GraphEdgeOut): number {
  if (e.proposed) return 0.55
  if (e.kind === 'linked') return TIER_STYLE[e.tier as Tier]?.opacity ?? 0.6
  return e.kind === 'within' ? 0.25 : 0.6
}

/** A node's radius: its type's size, growing slowly with how connected it is. */
export function nodeSize(n: GraphNodeOut): number {
  return NODE_STYLE[n.type].size + Math.min(6, Math.sqrt(n.weight))
}

export interface Filters {
  types: Set<NodeType>
  kinds: Set<EdgeKind>
  tiers: Set<Tier>
  /** Draw the links canopy's learning would make (needs `include=proposed` in the read). */
  proposed: boolean
  /** One workspace of the tree, or '' for all of them. */
  workspace: string
  /** Leave out what no kept edge touches. */
  hideIsolated: boolean
}

export function defaultFilters(): Filters {
  return {
    types: new Set(NODE_TYPES),
    kinds: new Set(EDGE_KINDS.filter((k) => k !== 'within')),
    tiers: new Set<Tier>(['core', 'adjacent', 'candidate']),
    proposed: false,
    workspace: '',
    hideIsolated: true,
  }
}

export interface View {
  nodes: GraphNodeOut[]
  edges: GraphEdgeOut[]
}

/** What the filters keep. A person has no workspace of their own, so a workspace
 *  filter keeps them when they touch something in it; an edge stays only when both
 *  its ends do. */
export function applyFilters(data: Pick<WorkspaceGraphOut, 'nodes' | 'edges'>, f: Filters): View {
  const inPlace = (n: GraphNodeOut) => !f.workspace || n.workspace === f.workspace || !n.workspace
  const typed = new Map(data.nodes.filter((n) => f.types.has(n.type) && inPlace(n)).map((n) => [n.id, n]))
  const edges = data.edges.filter((e) =>
    f.kinds.has(e.kind)
    && (e.kind !== 'linked' || f.tiers.has(e.tier as Tier))
    && (!e.proposed || f.proposed)
    && typed.has(e.source) && typed.has(e.target))
  // A person (no workspace) kept by a workspace filter must touch something that
  // lives in it: another person does not place them there.
  const placed = new Set<string>()
  for (const e of edges) {
    if (typed.get(e.source)!.workspace) placed.add(e.target)
    if (typed.get(e.target)!.workspace) placed.add(e.source)
  }
  const placedHere = (n: GraphNodeOut) => !f.workspace || !!n.workspace || placed.has(n.id)
  const placedNodes = new Set([...typed.values()].filter(placedHere).map((n) => n.id))
  const kept = edges.filter((e) => placedNodes.has(e.source) && placedNodes.has(e.target))
  const touched = new Set<string>()
  for (const e of kept) { touched.add(e.source); touched.add(e.target) }
  const nodes = [...typed.values()].filter((n) => placedNodes.has(n.id) && (!f.hideIsolated || touched.has(n.id)))
  return { nodes, edges: kept }
}

/** Each node's neighbours in `view`. */
export function adjacency(view: View): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>(view.nodes.map((n) => [n.id, new Set<string>()]))
  for (const e of view.edges) {
    out.get(e.source)?.add(e.target)
    out.get(e.target)?.add(e.source)
  }
  return out
}

/** `id` and everything within `depth` steps of it. */
export function neighbourhood(adj: Map<string, Set<string>>, id: string, depth = 1): Set<string> {
  const seen = new Set<string>([id])
  let frontier = [id]
  for (let d = 0; d < depth; d++) {
    const next: string[] = []
    for (const at of frontier) {
      for (const n of adj.get(at) ?? []) if (!seen.has(n)) { seen.add(n); next.push(n) }
    }
    frontier = next
  }
  return seen
}

/** Nodes whose label (or ref) has `query` in it: the ones that start with it first,
 *  then the most connected. */
export function searchNodes(nodes: readonly GraphNodeOut[], query: string, limit = 8): GraphNodeOut[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const hits = nodes.filter((n) => n.label.toLowerCase().includes(q) || n.ref.toLowerCase() === q)
  const starts = (n: GraphNodeOut) => (n.label.toLowerCase().startsWith(q) ? 0 : 1)
  return hits.sort((a, b) => starts(a) - starts(b) || b.weight - a.weight || a.label.localeCompare(b.label))
    .slice(0, limit)
}

/** The node's own page, or null (an output has none of its own). `here` is the page's
 *  workspace, for a person (a member page) who belongs to none in particular. */
export function hrefOf(n: GraphNodeOut, here: string): string | null {
  const at = (path: string) => scopedPath(n.workspace || here, path)
  switch (n.type) {
    case 'person': return scopedPath(here, `/members/${encodeURIComponent(n.ref)}`)
    case 'agent': return at(`/agents/${encodeURIComponent(n.ref)}`)
    case 'session': return at(`/sessions/${encodeURIComponent(n.ref)}`)
    case 'project': return at(`/projects/${encodeURIComponent(n.ref)}`)
    case 'task': return at(`/tasks/${encodeURIComponent(n.ref)}`)
    case 'workspace': return scopedPath(n.ref, '/network')
    default: return null
  }
}

/** What one edge says, from the selected node's side. */
export function describeEdge(e: GraphEdgeOut): string {
  const base = EDGE_STYLE[e.kind].label
  const tier = e.kind === 'linked' && e.tier ? ` · ${e.tier}${e.how ? ` (${e.how})` : ''}` : ''
  return `${e.proposed ? 'Would be: ' : ''}${base}${tier}`
}

/** The kinds and tiers present in `data`, so the panel offers only what is there. */
export function present(data: Pick<WorkspaceGraphOut, 'nodes' | 'edges'>): {
  types: Set<NodeType>; kinds: Set<EdgeKind>; tiers: Set<Tier>; workspaces: string[]
} {
  return {
    types: new Set(data.nodes.map((n) => n.type)),
    kinds: new Set(data.edges.map((e) => e.kind)),
    tiers: new Set(data.edges.filter((e) => e.kind === 'linked').map((e) => e.tier as Tier)),
    workspaces: [...new Set(data.nodes.filter((n) => n.type === 'workspace').map((n) => n.ref))].sort(),
  }
}
