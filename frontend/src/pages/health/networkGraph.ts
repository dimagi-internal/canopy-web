import type { SimulationLinkDatum, SimulationNodeDatum } from 'd3-force'

import type { NetworkLinkOut, NetworkNodeOut, WorkspaceNetworkOut } from '@/api/workspaces'
import { scopedPath } from '@/lib/scopedLinks'

// The pure half of the network view: what to draw, how big, what colour, where a
// click goes. The canvas and the force simulation live in WorkspaceNetworkPage.

export type ColorBy = 'kind' | 'channel' | 'workspace'

export interface GraphNode extends SimulationNodeDatum, NetworkNodeOut {
  r: number
}

export interface GraphLink extends SimulationLinkDatum<GraphNode> {
  kind: string
  turns: number
}

export interface Graph {
  nodes: GraphNode[]
  links: GraphLink[]
  byId: Map<string, GraphNode>
  /** Each node's neighbours: a person's threads, a thread's writers and agent. */
  neighbours: Map<string, Set<string>>
  /** Turns on the link between two nodes, either way round (`linkTurns`). */
  weights: Map<string, number>
}

const pair = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`)

/** Turns on the link between `a` and `b`: a person's turns in a thread, a thread's
 *  turns answered by its agent. */
export function linkTurns(g: Graph, a: string, b: string): number {
  return g.weights.get(pair(a, b)) ?? 0
}

/** Radius by kind and weight: actors are the dots you read, threads the mesh. */
export function radius(n: NetworkNodeOut): number {
  if (n.kind === 'thread') return 2 + Math.min(3, Math.log2(1 + n.turns) / 2)
  if (n.kind === 'agent') return 6 + Math.min(10, Math.sqrt(n.threads))
  return 4 + Math.min(6, Math.sqrt(n.threads))
}

export function buildGraph(data: WorkspaceNetworkOut): Graph {
  const nodes: GraphNode[] = data.nodes.map((n) => ({ ...n, r: radius(n) }))
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const neighbours = new Map<string, Set<string>>(nodes.map((n) => [n.id, new Set<string>()]))
  const links: GraphLink[] = []
  const weights = new Map<string, number>()
  for (const l of data.links as NetworkLinkOut[]) {
    if (!byId.has(l.source) || !byId.has(l.target)) continue
    links.push({ source: l.source, target: l.target, kind: l.kind, turns: l.turns })
    neighbours.get(l.source)!.add(l.target)
    neighbours.get(l.target)!.add(l.source)
    weights.set(pair(l.source, l.target), l.turns)
  }
  return { nodes, links, byId, neighbours, weights }
}

/** Where a click on a thread or agent goes; null for people and contacts. */
export function hrefOf(n: NetworkNodeOut): string | null {
  if (n.kind === 'agent' && n.ref) return scopedPath(n.workspace, `/agents/${encodeURIComponent(n.ref)}`)
  if (n.kind !== 'thread' || !n.ref) return null
  if (n.id.startsWith('mail:')) {
    return n.agent ? scopedPath(n.workspace, `/agents/${encodeURIComponent(n.agent)}/mail`) : null
  }
  return n.openable ? scopedPath(n.workspace, `/chat/${encodeURIComponent(n.ref)}`) : null
}

export const KIND_LABEL: Record<string, string> = {
  person: 'People', contact: 'Contacts', agent: 'Agents', thread: 'Threads',
}

/** CSS custom properties per kind — semantic tokens, read off the theme at draw time. */
export const KIND_TOKEN: Record<string, string> = {
  person: '--success', contact: '--warning', agent: '--special', thread: '--info',
}

// The theme's hued tokens only: --chart-2..5 are greys, which read as one colour.
const CATEGORY_TOKENS = ['--chart-1', '--info', '--success', '--special', '--warning', '--destructive']

/** A stable token per category (a channel or a workspace), in order of first sight. */
export function categoryTokens(values: readonly string[]): Map<string, string> {
  const out = new Map<string, string>()
  for (const v of values) if (!out.has(v)) out.set(v, CATEGORY_TOKENS[out.size % CATEGORY_TOKENS.length])
  return out
}

/** The token a node is drawn in. People, contacts and agents keep their kind's
 *  colour unless colouring by workspace; threads take the chosen category. */
export function tokenOf(n: NetworkNodeOut, by: ColorBy, cats: Map<string, string>): string {
  if (by === 'workspace') return cats.get(n.workspace) ?? KIND_TOKEN[n.kind]
  if (by === 'channel' && n.kind === 'thread') return cats.get(n.channel) ?? KIND_TOKEN.thread
  return KIND_TOKEN[n.kind] ?? '--muted-foreground'
}

/** Anchor points for "group by workspace": one per workspace, evenly on a circle. */
export function workspaceAnchors(workspaces: readonly string[], spread: number): Map<string, { x: number; y: number }> {
  const out = new Map<string, { x: number; y: number }>()
  if (workspaces.length <= 1) {
    for (const w of workspaces) out.set(w, { x: 0, y: 0 })
    return out
  }
  workspaces.forEach((w, i) => {
    const a = (2 * Math.PI * i) / workspaces.length
    out.set(w, { x: Math.cos(a) * spread, y: Math.sin(a) * spread })
  })
  return out
}
