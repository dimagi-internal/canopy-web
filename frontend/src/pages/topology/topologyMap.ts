import type {
  AgentEdgeOut,
  AgentTopologyAgentOut,
  AgentTopologyOut,
  RunnerTopologyOut,
  TopologyAgentOut,
  TopologyRunnerOut,
} from '@/api/workspaces'
import { edgeKey } from '../agentTopology'
import { agentHealth, type AgentHealth } from '../runnerTopology'

/** One agent on the map: its routing (runner topology) joined to who it is
 *  (agent topology). The two endpoints are read side by side and joined by slug. */
export interface MapAgent {
  slug: string
  name: string
  workspace: string
  ownerEmail: string | null
  loginEmail: string | null
  loginUserId: number | null
  interfacePublished: boolean
  fullPeople: readonly string[]
  turnMode: string
  routing: TopologyAgentOut | null
  health: AgentHealth
  /** Set when it has no runners of its own and follows a workspace's default order. */
  follows: TopologyAgentOut['follows']
  repoUrl: string
}

/** An owner's lane inside one workspace: the runners they paired that live
 *  there, and the agents they own there. `owner` null = "No owner set". */
export interface Lane {
  owner: string | null
  runners: TopologyRunnerOut[]
  agents: MapAgent[]
}

export interface MapWorkspace {
  slug: string
  displayName: string
  depth: number
  lanes: Lane[]
  children: MapWorkspace[]
  agentCount: number
  runnerCount: number
  /** Agents in this workspace whose turns cannot run now (unrouted or no live runner). */
  problemCount: number
  /** Its own default runner order (enabled runner ids, rank order); empty = none. */
  order: string[]
}

export interface FleetMap {
  root: MapWorkspace | null
  agents: Map<string, MapAgent>
  runners: Map<string, TopologyRunnerOut>
  /** Runners an agent here routes to that live outside the tree. */
  outside: TopologyRunnerOut[]
  edges: Map<string, AgentEdgeOut>
  /** The agent-topology rows as served, for the shared edge explanations. */
  profiles: Map<string, AgentTopologyAgentOut>
  /** For a workspace with no order of its own: the nearest one above it that has one. */
  orderFrom: Map<string, string | null>
}


function laneSort(a: Lane, b: Lane): number {
  // People first, alphabetically; the "No owner set" lane last.
  if (a.owner === null) return 1
  if (b.owner === null) return -1
  return a.owner.localeCompare(b.owner)
}

export function buildFleetMap(runnerTopo: RunnerTopologyOut, agentTopo: AgentTopologyOut): FleetMap {
  const runners = new Map(runnerTopo.runners.map((r) => [r.id, r] as const))
  const routing = new Map<string, TopologyAgentOut>()
  for (const ws of runnerTopo.workspaces) for (const a of ws.agents) routing.set(a.slug, a)

  const agents = new Map<string, MapAgent>()
  for (const a of agentTopo.agents) {
    const r = routing.get(a.slug) ?? null
    agents.set(a.slug, {
      slug: a.slug,
      name: a.name,
      workspace: a.workspace,
      ownerEmail: a.owner_email,
      loginEmail: a.login_email,
      loginUserId: a.login_user_id,
      interfacePublished: a.interface_published,
      fullPeople: a.full_people,
      turnMode: r?.turn_mode ?? '',
      routing: r,
      health: r ? agentHealth(r, runners) : 'unrouted',
      follows: r?.follows ?? null,
      repoUrl: r?.repo_url ?? '',
    })
  }

  const order = runnerTopo.workspaces
  const byParent = new Map<string | null, typeof order>()
  // runner-topology lists the tree depth-first; a workspace's parent is the
  // nearest earlier one at depth - 1.
  const stack: (typeof order)[number][] = []
  for (const ws of order) {
    while (stack.length && stack[stack.length - 1].depth >= ws.depth) stack.pop()
    const parent = stack.length ? stack[stack.length - 1].slug : null
    byParent.set(parent, [...(byParent.get(parent) ?? []), ws])
    stack.push(ws)
  }

  function lanesFor(slug: string): Lane[] {
    const lanes = new Map<string | null, Lane>()
    const lane = (owner: string | null) => {
      let l = lanes.get(owner)
      if (!l) lanes.set(owner, (l = { owner, runners: [], agents: [] }))
      return l
    }
    for (const r of runnerTopo.runners) if (r.in_tree && r.workspace === slug) lane(r.owner_email).runners.push(r)
    for (const a of agents.values()) if (a.workspace === slug) lane(a.ownerEmail).agents.push(a)
    return [...lanes.values()].sort(laneSort)
  }

  function build(ws: (typeof order)[number]): MapWorkspace {
    const lanes = lanesFor(ws.slug)
    const children = (byParent.get(ws.slug) ?? []).map(build)
    const own = lanes.flatMap((l) => l.agents)
    return {
      slug: ws.slug,
      displayName: ws.display_name,
      depth: ws.depth,
      lanes,
      children,
      agentCount: own.length + children.reduce((n, c) => n + c.agentCount, 0),
      runnerCount: lanes.reduce((n, l) => n + l.runners.length, 0) + children.reduce((n, c) => n + c.runnerCount, 0),
      problemCount: own.filter((a) => a.health !== 'ok').length + children.reduce((n, c) => n + c.problemCount, 0),
      order: (ws.order ?? []).filter((o) => o.enabled).map((o) => o.runner_id),
    }
  }

  const top = byParent.get(null) ?? []
  return {
    root: top.length ? build(top[0]) : null,
    agents,
    runners,
    outside: runnerTopo.runners.filter((r) => !r.in_tree),
    edges: new Map(agentTopo.edges.map((e) => [edgeKey(e.source, e.target), e] as const)),
    profiles: new Map(agentTopo.agents.map((a) => [a.slug, a] as const)),
    orderFrom: new Map(runnerTopo.workspaces.map((w) => [w.slug, w.order_from ?? null] as const)),
  }
}

/** Default collapsed state: a workspace with nothing in it starts closed. */
export function initiallyCollapsed(ws: MapWorkspace, out = new Set<string>()): Set<string> {
  if (ws.agentCount === 0 && ws.runnerCount === 0 && ws.depth > 0) out.add(ws.slug)
  ws.children.forEach((c) => initiallyCollapsed(c, out))
  return out
}

/** The collapsed workspace hiding things that live in `workspace`: the
 *  outermost collapsed box on the path from the root down to it (itself
 *  included), or null when it is in view. Arrows to a hidden agent land there. */
export function hiddenBy(root: MapWorkspace, workspace: string, collapsed: Set<string>): string | null {
  const path = pathTo(root, workspace)
  return path?.find((slug) => collapsed.has(slug)) ?? null
}

function pathTo(ws: MapWorkspace, slug: string): string[] | null {
  if (ws.slug === slug) return [ws.slug]
  for (const c of ws.children) {
    const rest = pathTo(c, slug)
    if (rest) return [ws.slug, ...rest]
  }
  return null
}

/** Short runner name for chips: drop the `-cdp` / `-mbp-cdp` suffixes people
 *  never say out loud. */
export function shortRunner(name: string): string {
  return name.replace(/-mbp-cdp$/, '').replace(/-cdp$/, '')
}

export function initials(email: string | null): string {
  if (!email) return '—'
  const local = email.split('@')[0]
  const parts = local.split(/[._-]+/).filter(Boolean)
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : local.slice(0, 2)).toUpperCase()
}

export type Rect = { x: number; y: number; w: number; h: number }

/** A gentle curve from the edge of `a` to the edge of `b`, bowed sideways so
 *  arrows that share a direction do not sit on top of each other. */
export function curve(a: Rect, b: Rect): { d: string; labelX: number; labelY: number } | null {
  const ca = { x: a.x + a.w / 2, y: a.y + a.h / 2 }
  const cb = { x: b.x + b.w / 2, y: b.y + b.h / 2 }
  const dx = cb.x - ca.x
  const dy = cb.y - ca.y
  if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return null
  const edge = (c: { x: number; y: number }, r: Rect, sx: number, sy: number) => {
    const t = Math.min(
      sx === 0 ? Infinity : r.w / 2 / Math.abs(sx),
      sy === 0 ? Infinity : r.h / 2 / Math.abs(sy),
    )
    return { x: c.x + sx * t, y: c.y + sy * t }
  }
  const p0 = edge(ca, a, dx, dy)
  const p2 = edge(cb, b, -dx, -dy)
  const len = Math.hypot(p2.x - p0.x, p2.y - p0.y)
  const bow = Math.min(60, len * 0.18)
  const mx = (p0.x + p2.x) / 2
  const my = (p0.y + p2.y) / 2
  const nx = -(p2.y - p0.y) / (len || 1)
  const ny = (p2.x - p0.x) / (len || 1)
  const c = { x: mx + nx * bow, y: my + ny * bow }
  const r = (n: number) => Math.round(n * 10) / 10
  const at = (t: number, a0: number, a1: number, a2: number) => (1 - t) ** 2 * a0 + 2 * (1 - t) * t * a1 + t ** 2 * a2
  return {
    d: `M ${r(p0.x)} ${r(p0.y)} Q ${r(c.x)} ${r(c.y)} ${r(p2.x)} ${r(p2.y)}`,
    // Labelled near the head, not the middle: arrows fanning out of one card
    // share their first half, so mid-point labels pile onto each other.
    labelX: r(at(0.78, p0.x, c.x, p2.x)),
    labelY: r(at(0.78, p0.y, c.y, p2.y) - 6),
  }
}

/** The canopy-web the runner pairs with when told nothing else
 *  (runner/canopy_runner/canopy_runner/pair.py::DEFAULT_BASE_URL). */
export const RUNNER_DEFAULT_BASE_URL = 'https://labs.connect.dimagi.com/canopy'

/** The one command that pairs a new laptop runner into `workspace`, as run on
 *  that macOS account (runner/canopy_runner/README.md, "One-time laptop setup").
 *  The installer fetches the checkout and builds from origin/main itself, then
 *  pairs AS the PAT on that account — so the box lands in that person's lane.
 *  `--base-url` only when this deployment is not the runner's default. */
export function pairingCommand(workspace: string, baseUrl: string): string {
  const base = baseUrl.replace(/\/+$/, '')
  const cmd = `~/emdash-projects/canopy-web/runner/canopy_runner/scripts/install-runner.sh --workspace ${workspace}`
  return base && base !== RUNNER_DEFAULT_BASE_URL ? `${cmd} --base-url ${base}` : cmd
}
