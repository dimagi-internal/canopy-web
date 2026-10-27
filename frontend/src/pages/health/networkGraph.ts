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
  /** thread -> its writers' turns there; writer -> their turns per thread. */
  writersOf: Map<string, Map<string, number>>
  threadsOf: Map<string, Map<string, number>>
  /** thread -> the agent that answered it, when that is not also a writer. */
  agentOf: Map<string, string>
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
  const writersOf = new Map<string, Map<string, number>>()
  const threadsOf = new Map<string, Map<string, number>>()
  const agentOf = new Map<string, string>()
  const put = (m: Map<string, Map<string, number>>, a: string, b: string, n: number) => {
    if (!m.has(a)) m.set(a, new Map())
    m.get(a)!.set(b, n)
  }
  for (const l of data.links as NetworkLinkOut[]) {
    if (!byId.has(l.source) || !byId.has(l.target)) continue
    links.push({ source: l.source, target: l.target, kind: l.kind, turns: l.turns })
    neighbours.get(l.source)!.add(l.target)
    neighbours.get(l.target)!.add(l.source)
    weights.set(pair(l.source, l.target), l.turns)
    if (l.kind === 'answered') agentOf.set(l.source, l.target)
    else {
      put(writersOf, l.target, l.source, l.turns)
      put(threadsOf, l.source, l.target, l.turns)
    }
  }
  return { nodes, links, byId, neighbours, weights, writersOf, threadsOf, agentOf }
}

export interface Tally { node: GraphNode; turns: number; threads: number }

export interface ThreadRow {
  node: GraphNode
  /** The selected node's turns here (a writer's), or the thread's own (an agent's). */
  turns: number
  agent: GraphNode | null
  /** Other people and contacts who wrote here, besides the selected one. */
  others: number
}

/** What the side panel says about one node: who it works with, through which agents
 *  and channels, and its threads — all read off the graph already drawn. */
export interface Profile {
  /** People and contacts in the same threads (a writer), or who use it (an agent). */
  people: Tally[]
  /** Agents its threads went to (a writer), or that wrote in them (an agent). */
  agents: Tally[]
  /** Threads per channel. */
  channels: { channel: string; threads: number }[]
  threads: ThreadRow[]
  /** A thread's writers, with their turns there. */
  writers: Tally[]
}

const human = (n: GraphNode | undefined) => n?.kind === 'person' || n?.kind === 'contact'

export function profile(g: Graph, id: string): Profile {
  const self = g.byId.get(id)
  const out: Profile = { people: [], agents: [], channels: [], threads: [], writers: [] }
  if (!self) return out
  if (self.kind === 'thread') {
    out.writers = [...(g.writersOf.get(id) ?? [])]
      .map(([w, turns]) => ({ node: g.byId.get(w)!, turns, threads: 1 }))
      .sort((a, b) => b.turns - a.turns)
    return out
  }
  // An agent's threads are the ones it answered plus any it wrote in.
  const mine = new Map(g.threadsOf.get(id) ?? [])
  if (self.kind === 'agent') {
    for (const [t, a] of g.agentOf) if (a === id && !mine.has(t)) mine.set(t, g.byId.get(t)!.turns)
  }
  const people = new Map<string, Tally>()
  const agents = new Map<string, Tally>()
  const channels = new Map<string, number>()
  const bump = (m: Map<string, Tally>, nid: string, turns: number) => {
    const n = g.byId.get(nid)
    if (!n) return
    const t = m.get(nid) ?? { node: n, turns: 0, threads: 0 }
    t.turns += turns
    t.threads += 1
    m.set(nid, t)
  }
  for (const [tid, turns] of mine) {
    const thread = g.byId.get(tid)
    if (!thread) continue
    const writers = g.writersOf.get(tid) ?? new Map<string, number>()
    const agentId = g.agentOf.get(tid) ?? (self.kind === 'agent' ? id : null)
    let others = 0
    for (const [w, wt] of writers) {
      if (w === id) continue
      if (human(g.byId.get(w))) {
        others += 1
        // Beside a writer, turns are theirs in the shared threads; beside an agent, the
        // writer's turns to it — so only in threads it answered, not ones it wrote in.
        if (self.kind !== 'agent' || agentId === id) bump(people, w, wt)
      } else bump(agents, w, wt)
    }
    if (agentId && agentId !== id) bump(agents, agentId, turns)
    channels.set(thread.channel || 'other', (channels.get(thread.channel || 'other') ?? 0) + 1)
    out.threads.push({ node: thread, turns, agent: agentId ? g.byId.get(agentId) ?? null : null, others })
  }
  const byWeight = (a: Tally, b: Tally) => b.threads - a.threads || b.turns - a.turns
  out.people = [...people.values()].sort(byWeight)
  out.agents = [...agents.values()].sort(byWeight)
  out.channels = [...channels].map(([channel, threads]) => ({ channel, threads })).sort((a, b) => b.threads - a.threads)
  out.threads.sort((a, b) => b.turns - a.turns || (b.node.last_at ?? '').localeCompare(a.node.last_at ?? ''))
  return out
}

/** Where a click on a thread or agent goes; null for people and contacts. */
export function hrefOf(n: NetworkNodeOut): string | null {
  if (n.kind === 'agent' && n.ref) return scopedPath(n.workspace, `/agents/${encodeURIComponent(n.ref)}`)
  if (n.kind !== 'thread' || !n.ref) return null
  if (n.id.startsWith('mail:')) {
    return n.agent ? scopedPath(n.workspace, `/agents/${encodeURIComponent(n.agent)}/mail`) : null
  }
  return n.openable ? scopedPath(n.workspace, `/sessions/${encodeURIComponent(n.ref)}`) : null
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
