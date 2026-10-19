import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type Simulation,
} from 'd3-force'

import { getWorkspaceNetwork, type WorkspaceNetworkOut } from '@/api/workspaces'
import { AdminOnly, HealthViews } from './HealthViews'
import {
  buildGraph, categoryTokens, hrefOf, KIND_LABEL, KIND_TOKEN, linkTurns, tokenOf, workspaceAnchors,
  type ColorBy, type Graph, type GraphLink, type GraphNode,
} from './networkGraph'
import { useTreeRead } from './useTreeRead'

// WORKSPACE NETWORK: people, contacts and agents, each linked to the threads they
// wrote in (a chat — where a bound Slack thread lives too — or an email thread),
// and each thread to the agent that answered it. Is an agent a room people meet
// in, or a private line? Read from the analytics fact log
// (`GET /api/workspaces/{slug}/network`); a chat's title shows only to someone
// who may open it.

const RANGES = [7, 30, 90] as const
const HEIGHT = 620

export function WorkspaceNetworkPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [days, setDays] = useState<number>(30)
  const [tree, setTree] = useState(true)
  const { data, error, forbidden } = useTreeRead(
    () => getWorkspaceNetwork(slug, { days, tree }), `${slug}:${days}:${tree}`)

  return (
    <div>
      <HealthViews title="Network" />
      {!forbidden && <div className="mb-4 flex flex-wrap items-center gap-2 text-[12px]">
        <div className="inline-flex gap-0.5 rounded-lg border border-border bg-card p-0.5" role="group" aria-label="Range">
          {RANGES.map((n) => (
            <button key={n} type="button" onClick={() => setDays(n)} aria-pressed={days === n}
                    className={`min-h-8 rounded-md px-2.5 ${days === n ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground'}`}>
              {n} days
            </button>
          ))}
        </div>
        <label className="inline-flex min-h-8 items-center gap-1.5 text-foreground-secondary">
          <input type="checkbox" checked={tree} onChange={(e) => setTree(e.target.checked)} />
          Include workspaces below
        </label>
      </div>}
      {forbidden ? <AdminOnly what="the workspace network" />
        : error ? <p className="text-[13px] text-destructive">{error}</p>
          : !data ? <p className="text-[13px] text-muted-foreground">Loading…</p>
            : <Network data={data} />}
    </div>
  )
}

function Network({ data }: { data: WorkspaceNetworkOut }): JSX.Element {
  const t = data.totals
  const graph = useMemo(() => buildGraph(data), [data])
  const [colorBy, setColorBy] = useState<ColorBy>('kind')
  const [group, setGroup] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const cats = useMemo(() => categoryTokens(
    colorBy === 'workspace' ? graph.nodes.map((n) => n.workspace)
      : graph.nodes.filter((n) => n.kind === 'thread').map((n) => n.channel || 'other')), [graph, colorBy])
  const selectedNode = selected ? graph.byId.get(selected) ?? null : null

  return (
    <div className="space-y-4">
      <section aria-label="Totals" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="People" value={t.people} token="--success" />
        <Stat label="Contacts" value={t.contacts} token="--warning" />
        <Stat label="Agents" value={t.agents} token="--special" />
        <Stat label="Threads" value={t.threads} token="--info" />
        <Stat label="Threads with 2+ people" value={t.shared_threads} />
        <Stat label="Wrote in the same thread" value={t.shared_pairs} hint="pairs of people or contacts" />
      </section>
      {data.truncated && (
        <p className="text-[12px] text-muted-foreground">
          Showing the {t.threads.toLocaleString()} busiest of {t.threads_seen.toLocaleString()} threads — shared ones first.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3 text-[12px]">
        <div className="inline-flex gap-0.5 rounded-lg border border-border bg-card p-0.5" role="group" aria-label="Colour by">
          {(['kind', 'channel', 'workspace'] as const).map((c) => (
            <button key={c} type="button" onClick={() => setColorBy(c)} aria-pressed={colorBy === c}
                    className={`min-h-8 rounded-md px-2.5 ${colorBy === c ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground'}`}>
              {c === 'kind' ? 'Colour: who' : c === 'channel' ? 'Colour: channel' : 'Colour: workspace'}
            </button>
          ))}
        </div>
        <label className="inline-flex min-h-8 items-center gap-1.5 text-foreground-secondary">
          <input type="checkbox" checked={group} onChange={(e) => setGroup(e.target.checked)} />
          Group by workspace
        </label>
        <Legend colorBy={colorBy} cats={cats} />
      </div>
      {graph.nodes.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No one wrote in a thread in this window.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          <Canvas graph={graph} colorBy={colorBy} cats={cats} group={group}
                  workspaces={data.workspaces} selected={selected} onSelect={setSelected} />
          <Detail node={selectedNode} graph={graph} onSelect={setSelected} />
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, token, hint }: { label: string; value: number; token?: string; hint?: string }): JSX.Element {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2" title={hint}>
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        {token && <span className="inline-block size-2 rounded-full" style={{ background: `var(${token})` }} />}
        {label}
      </div>
      <div className="text-lg font-semibold tabular-nums text-foreground">{value.toLocaleString()}</div>
    </div>
  )
}

function Legend({ colorBy, cats }: { colorBy: ColorBy; cats: Map<string, string> }): JSX.Element {
  const items: [string, string][] = colorBy === 'kind'
    ? Object.entries(KIND_TOKEN).map(([k, tok]) => [KIND_LABEL[k], tok])
    : [...cats.entries()]
  return (
    <ul className="flex flex-wrap items-center gap-x-3 gap-y-1 text-muted-foreground" aria-label="Legend">
      {items.map(([label, tok]) => (
        <li key={label} className="inline-flex items-center gap-1">
          <span className="inline-block size-2 rounded-full" style={{ background: `var(${tok})` }} />{label}
        </li>
      ))}
      {colorBy === 'channel' && <li>· people, contacts and agents keep their colour</li>}
    </ul>
  )
}

interface View { x: number; y: number; k: number }

function Canvas({ graph, colorBy, cats, group, workspaces, selected, onSelect }: {
  graph: Graph; colorBy: ColorBy; cats: Map<string, string>; group: boolean; workspaces: readonly string[]
  selected: string | null; onSelect: (id: string | null) => void
}): JSX.Element {
  const navigate = useNavigate()
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const simRef = useRef<Simulation<GraphNode, GraphLink> | null>(null)
  const viewRef = useRef<View>({ x: 0, y: 0, k: 1 })
  const drawRef = useRef<() => void>(() => {})
  // Set once the viewer zooms or pans; until then the view fits the graph.
  const movedRef = useRef(false)
  const [hover, setHover] = useState<{ node: GraphNode; x: number; y: number } | null>(null)

  // Drawing: re-made whenever what it depends on changes, called by the
  // simulation's ticks and by pan/zoom.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const css = getComputedStyle(document.documentElement)
    const colour = new Map<string, string>()
    const read = (tok: string) => {
      if (!colour.has(tok)) colour.set(tok, css.getPropertyValue(tok).trim() || 'gray')
      return colour.get(tok)!
    }
    const lit = selected ? new Set([selected, ...(graph.neighbours.get(selected) ?? [])]) : null
    drawRef.current = () => {
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const dpr = window.devicePixelRatio || 1
      const { x, y, k } = viewRef.current
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (canvas.width / dpr / 2 + x), dpr * (HEIGHT / 2 + y))
      ctx.lineWidth = 0.6 / k
      ctx.strokeStyle = read('--muted-foreground')
      for (const l of graph.links) {
        const s = l.source as GraphNode
        const d = l.target as GraphNode
        const on = !lit || (lit.has(s.id) && lit.has(d.id))
        ctx.globalAlpha = on ? (lit ? 0.7 : 0.18) : 0.04
        ctx.beginPath()
        ctx.moveTo(s.x ?? 0, s.y ?? 0)
        ctx.lineTo(d.x ?? 0, d.y ?? 0)
        ctx.stroke()
      }
      for (const n of graph.nodes) {
        ctx.globalAlpha = !lit || lit.has(n.id) ? (n.kind === 'thread' ? 0.85 : 1) : 0.12
        ctx.fillStyle = read(tokenOf(n, colorBy, cats))
        ctx.beginPath()
        ctx.arc(n.x ?? 0, n.y ?? 0, n.r, 0, 2 * Math.PI)
        ctx.fill()
        if (n.id === selected) {
          ctx.lineWidth = 2 / k
          ctx.strokeStyle = read('--foreground')
          ctx.stroke()
          ctx.lineWidth = 0.6 / k
          ctx.strokeStyle = read('--muted-foreground')
        }
      }
      // Names on the actors you are looking at: agents always, people when zoomed or lit.
      ctx.globalAlpha = 1
      ctx.fillStyle = read('--foreground')
      ctx.font = `${11 / k}px sans-serif`
      for (const n of graph.nodes) {
        if (n.kind === 'thread') continue
        const show = lit ? lit.has(n.id) : n.kind === 'agent' || k > 1.6
        if (show) ctx.fillText(n.label, (n.x ?? 0) + n.r + 2 / k, (n.y ?? 0) + 3 / k)
      }
    }
    drawRef.current()
  }, [graph, colorBy, cats, selected])

  // The simulation: one per graph; grouping only changes its pull.
  useEffect(() => {
    const sim = forceSimulation<GraphNode, GraphLink>(graph.nodes)
      .force('link', forceLink<GraphNode, GraphLink>(graph.links).id((n) => n.id)
        .distance((l) => (l.kind === 'answered' ? 40 : 18)).strength(0.5))
      .force('charge', forceManyBody<GraphNode>().strength((n) => (n.kind === 'thread' ? -6 : -60)).distanceMax(300))
      .force('collide', forceCollide<GraphNode>((n) => n.r + 1))
      .on('tick', () => drawRef.current())
      .on('end', () => {
        const canvas = canvasRef.current
        if (movedRef.current || !canvas || graph.nodes.length === 0) return
        const xs = graph.nodes.map((n) => n.x ?? 0)
        const ys = graph.nodes.map((n) => n.y ?? 0)
        const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
        const width = canvas.width / (window.devicePixelRatio || 1)
        const k = Math.min(4, (width - 60) / Math.max(1, x1 - x0), (HEIGHT - 80) / Math.max(1, y1 - y0))
        viewRef.current = { k, x: -((x0 + x1) / 2) * k, y: -((y0 + y1) / 2) * k }
        drawRef.current()
      })
    simRef.current = sim
    return () => { sim.stop() }
  }, [graph])

  useEffect(() => {
    const sim = simRef.current
    if (!sim) return
    const anchors = workspaceAnchors(group ? workspaces : [], 140 * Math.sqrt(workspaces.length))
    const at = (n: GraphNode) => anchors.get(n.workspace) ?? { x: 0, y: 0 }
    // Threads belong to exactly one workspace and pull hard; people and agents
    // span workspaces, so they sit between the clusters they work in.
    const pull = (n: GraphNode) => (!group ? 0.04 : n.kind === 'thread' ? 0.3 : 0.05)
    sim.force('x', forceX<GraphNode>((n) => at(n).x).strength(pull))
      .force('y', forceY<GraphNode>((n) => at(n).y).strength(pull))
      .alpha(0.6).restart()
  }, [group, workspaces, graph])

  // Size to the container, at device resolution.
  useEffect(() => {
    const wrap = wrapRef.current
    const canvas = canvasRef.current
    if (!wrap || !canvas) return
    const fit = () => {
      const dpr = window.devicePixelRatio || 1
      canvas.width = wrap.clientWidth * dpr
      canvas.height = HEIGHT * dpr
      canvas.style.width = `${wrap.clientWidth}px`
      canvas.style.height = `${HEIGHT}px`
      drawRef.current()
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [])

  // Screen point -> graph point, and the node under it.
  const toGraph = (cx: number, cy: number) => {
    const canvas = canvasRef.current!
    const rect = canvas.getBoundingClientRect()
    const { x, y, k } = viewRef.current
    return { gx: (cx - rect.left - rect.width / 2 - x) / k, gy: (cy - rect.top - HEIGHT / 2 - y) / k }
  }
  const nodeAt = (cx: number, cy: number): GraphNode | null => {
    const { gx, gy } = toGraph(cx, cy)
    const k = viewRef.current.k
    let best: GraphNode | null = null
    let bestD = Infinity
    for (const n of graph.nodes) {
      const d = Math.hypot((n.x ?? 0) - gx, (n.y ?? 0) - gy)
      if (d <= n.r + 4 / k && d < bestD) { best = n; bestD = d }
    }
    return best
  }

  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const v = viewRef.current
      const rect = canvas.getBoundingClientRect()
      const mx = e.clientX - rect.left - rect.width / 2
      const my = e.clientY - rect.top - HEIGHT / 2
      const k = Math.max(0.2, Math.min(8, v.k * Math.exp(-e.deltaY * 0.0015)))
      movedRef.current = true
      viewRef.current = { k, x: mx - ((mx - v.x) * k) / v.k, y: my - ((my - v.y) * k) / v.k }
      drawRef.current()
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [])

  return (
    <div ref={wrapRef} className="relative overflow-hidden rounded-lg border border-border bg-card">
      <canvas
        ref={canvasRef}
        role="img"
        aria-label="Network of people, contacts and agents linked by the threads they wrote in"
        className="block cursor-grab active:cursor-grabbing"
        onPointerDown={(e) => {
          drag.current = { x: e.clientX, y: e.clientY, moved: false }
          // Keeps a pan going past the edge; a pointer the browser does not track
          // (a synthetic one) has nothing to capture.
          try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* nothing to capture */ }
        }}
        onPointerMove={(e) => {
          const d = drag.current
          if (d) {
            const dx = e.clientX - d.x
            const dy = e.clientY - d.y
            if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true
            if (d.moved) {
              movedRef.current = true
              viewRef.current = { ...viewRef.current, x: viewRef.current.x + dx, y: viewRef.current.y + dy }
              d.x = e.clientX
              d.y = e.clientY
              drawRef.current()
            }
            return
          }
          const n = nodeAt(e.clientX, e.clientY)
          const rect = e.currentTarget.getBoundingClientRect()
          setHover(n ? { node: n, x: e.clientX - rect.left, y: e.clientY - rect.top } : null)
        }}
        onPointerUp={(e) => {
          const d = drag.current
          drag.current = null
          if (d?.moved) return
          const n = nodeAt(e.clientX, e.clientY)
          if (!n) { onSelect(null); return }
          // A second click on a selected thread opens it.
          const href = hrefOf(n)
          if (n.id === selected && href) navigate(href)
          else onSelect(n.id)
        }}
        onPointerLeave={() => setHover(null)}
      />
      {hover && (
        <div className="pointer-events-none absolute z-10 max-w-64 rounded-md border border-border bg-popover px-2 py-1 text-[12px] text-popover-foreground shadow"
             style={{ left: Math.min(hover.x + 12, 9999), top: hover.y + 12 }}>
          <div className="font-medium">{hover.node.label}</div>
          <div className="text-muted-foreground">{describe(hover.node)}</div>
        </div>
      )}
      <p className="absolute bottom-2 left-3 text-[11px] text-muted-foreground">
        Scroll to zoom, drag to pan. Click a dot to see its threads; click a selected thread again to open it.
      </p>
    </div>
  )
}

function describe(n: GraphNode): string {
  if (n.kind === 'thread') {
    return `${n.channel || 'chat'} · ${n.turns} turn${n.turns === 1 ? '' : 's'} · ${n.workspace}`
  }
  const kind = n.kind === 'person' ? 'Person' : n.kind === 'contact' ? 'Contact' : 'Agent'
  return `${kind} · ${n.threads} thread${n.threads === 1 ? '' : 's'} · ${n.turns} turns · ${n.workspaces.join(', ')}`
}

function Detail({ node, graph, onSelect }: {
  node: GraphNode | null; graph: Graph; onSelect: (id: string | null) => void
}): JSX.Element {
  if (!node) {
    return (
      <aside className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground">
        Click a person, contact, agent or thread to see who it is connected to.
      </aside>
    )
  }
  const href = hrefOf(node)
  // Each neighbour with the turns on its link to this node: a person's turns in
  // this thread, or this person's turns in each of their threads.
  const around = [...(graph.neighbours.get(node.id) ?? [])]
    .map((id) => ({ n: graph.byId.get(id)!, turns: linkTurns(graph, node.id, id) }))
    .sort((a, b) => b.turns - a.turns)
  return (
    <aside className="rounded-lg border border-border bg-card p-3 text-[13px]" aria-label="Selected">
      <div className="mb-1 flex items-start justify-between gap-2">
        <h2 className="font-semibold text-foreground">{node.label}</h2>
        <button type="button" onClick={() => onSelect(null)} className="text-[12px] text-muted-foreground hover:text-foreground">Clear</button>
      </div>
      <p className="mb-2 text-[12px] text-muted-foreground">{describe(node)}</p>
      {href ? (
        <Link to={href} className="mb-3 inline-block text-[12px] text-primary hover:underline">
          {node.kind === 'agent' ? 'Open agent' : 'Open thread'} →
        </Link>
      ) : node.kind === 'thread' ? (
        <p className="mb-3 text-[12px] text-muted-foreground">You are not in this chat, so it does not open for you.</p>
      ) : null}
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="py-1 font-normal">{node.kind === 'thread' ? 'Who' : 'Threads'}</th>
            <th className="py-1 text-right font-normal">{node.kind === 'thread' ? 'Turns here' : 'Their turns'}</th>
          </tr>
        </thead>
        <tbody>
          {around.slice(0, 50).map(({ n, turns }) => {
            const to = hrefOf(n)
            return (
              <tr key={n.id} className="border-t border-border">
                <td className="py-1">
                  <button type="button" onClick={() => onSelect(n.id)} className="text-left text-foreground hover:underline">
                    <span className="mr-1.5 inline-block size-2 rounded-full" style={{ background: `var(${KIND_TOKEN[n.kind]})` }} />
                    {n.label}
                  </button>
                  {to && n.kind === 'thread' && (
                    <Link to={to} className="ml-1.5 text-muted-foreground hover:text-foreground" aria-label={`Open ${n.label}`}>↗</Link>
                  )}
                </td>
                <td className="py-1 text-right tabular-nums text-foreground-secondary">{turns}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {around.length > 50 && <p className="mt-1 text-[11px] text-muted-foreground">and {around.length - 50} more</p>}
    </aside>
  )
}
