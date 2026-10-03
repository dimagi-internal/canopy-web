import { useCallback, useEffect, useLayoutEffect, useRef, useState, type JSX } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { Check, ChevronDown, ChevronRight, Copy, Plus } from 'lucide-react'

import { grantAgentAdmin, revokeAgentAdmin, type TurnMode } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { useAuth } from '@/auth/AuthProvider'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { RunnerDetail } from '@/components/supervisor/RunnerDetail'
import {
  getAgentTopology,
  getRunnerTopology,
  WorkspaceApiError,
  type AgentEdgeOut,
  type TopologyRunnerOut,
} from '@/api/workspaces'
import { edgeKey, explainEdge } from '../agentTopology'
import { defaultRoutes, dependsSolelyOn, ruleRoutes, statusTone } from '../runnerTopology'
import { TopologyViews } from './TopologyViews'
import { WorkspaceOrderPanel } from './WorkspaceOrderPanel'
import {
  buildFleetMap,
  curve,
  hiddenBy,
  initials,
  initiallyCollapsed,
  pairingCommand,
  shortRunner,
  type FleetMap,
  type Lane,
  type MapAgent,
  type MapWorkspace,
} from './topologyMap'

// THE FLEET MAP: workspaces as nested boxes, each holding a lane per owner with
// that owner's runners and agents, and — for the selected agent — arrows to
// every other agent it can send work to.
//
// The Runners and Agent access tables answer one question each; the trouble
// they were built for (Ada→Hal failing for a week) lived in the JOIN of four
// facts on four screens: the tree, who owns what, which box an agent runs on,
// and what each agent's login gets from each other agent. This puts all four
// on one picture, read from the same two endpoints the tables use.
//
// Each workspace collapses; an empty one starts collapsed. Arrows to an agent
// inside a collapsed box land on the box, so collapsing hides detail, never a
// relationship. Selection lives in the URL (?agent= / ?runner=) so a link names
// what it is about.
//
// It is also where runners are CONFIGURED: the panel mounts the supervisor's own
// runner detail (login, admins, flags, drills, pause, retire) and the agent's own
// routing table, so there is one implementation of each and two doors onto it;
// every control gates on what the server says the viewer may do (can_manage /
// can_administer, the routing PUT's own check). An owner lane's "Add a runner"
// gives the pairing command, since pairing is something you run on the box.

type Selection =
  | { kind: 'agent'; slug: string }
  | { kind: 'runner'; id: string }
  | { kind: 'add'; workspace: string; owner: string }
  | { kind: 'order'; workspace: string }
  | null

const HEALTH = {
  ok: null,
  down: { text: 'No live runner', className: 'text-destructive' },
  unrouted: { text: 'Unrouted', className: 'text-destructive' },
} as const

export function TopologyMapPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  return <FleetMapView key={slug} slug={slug} />
}

function FleetMapView({ slug }: { slug: string }): JSX.Element {
  const [map, setMap] = useState<FleetMap | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [params, setParams] = useSearchParams()
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  // The fleet as GET /harness/runners/ serves it: the RunnerOut (with can_manage /
  // can_administer) the runner detail gates on. The topology rows do not carry
  // those, and must not — they answer "what is wired", not "what may I change".
  const [fleet, setFleet] = useState<Map<string, RunnerOut>>(new Map())
  const loadFleet = useCallback(
    () =>
      listRunners()
        .then((rs) => setFleet(new Map(rs.map((r) => [r.id, r] as const))))
        // The map still works without it; the panel then says where to look.
        .catch(() => setFleet(new Map())),
    [],
  )
  useEffect(() => { void loadFleet() }, [loadFleet])

  const load = useCallback(
    () => Promise.all([getRunnerTopology(slug), getAgentTopology(slug)]).then(([r, a]) => buildFleetMap(r, a)),
    [slug],
  )

  useEffect(() => {
    let off = false
    load()
      .then((m) => {
        if (off) return
        setMap(m)
        if (m.root) setCollapsed(initiallyCollapsed(m.root))
      })
      .catch((e: unknown) => {
        if (off) return
        if (e instanceof WorkspaceApiError && (e.status === 403 || e.status === 404)) setForbidden(true)
        else setError(e instanceof Error ? e.message : 'Could not load the fleet map')
      })
    return () => { off = true }
  }, [load])

  const agentParam = params.get('agent')
  const runnerParam = params.get('runner')
  const addParam = params.get('add')
  const orderParam = params.get('order')
  const selection: Selection = orderParam
    ? { kind: 'order', workspace: orderParam }
    : agentParam
    ? { kind: 'agent', slug: agentParam }
    : runnerParam
      ? { kind: 'runner', id: runnerParam }
      : addParam
        ? { kind: 'add', workspace: addParam, owner: params.get('owner') ?? '' }
        : null

  const select = (next: Selection) => {
    const p = new URLSearchParams(params)
    for (const k of ['agent', 'runner', 'add', 'owner', 'order']) p.delete(k)
    if (next?.kind === 'agent') p.set('agent', next.slug)
    if (next?.kind === 'runner') p.set('runner', next.id)
    if (next?.kind === 'order') p.set('order', next.workspace)
    if (next?.kind === 'add') {
      p.set('add', next.workspace)
      p.set('owner', next.owner)
    }
    setParams(p, { replace: true })
  }

  // A change made in the panel redraws the map from the server, keeping what is
  // collapsed — the picture must not disagree with the control beside it.
  const refresh = useCallback(() => {
    load().then(setMap).catch(() => { /* keep the last good picture */ })
    void loadFleet()
  }, [load, loadFleet])

  const toggle = (ws: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(ws)) next.delete(ws)
      else next.add(ws)
      return next
    })

  async function act(run: () => Promise<unknown>) {
    setBusy(true)
    setActionError(null)
    try {
      await run()
      setMap(await load())
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  if (forbidden) {
    return (
      <div>
        <TopologyViews />
        <p className="text-[13px] text-muted-foreground" data-testid="map-forbidden">
          Only a workspace admin or owner can see the fleet map.
        </p>
      </div>
    )
  }
  if (error) return <div><TopologyViews /><p className="text-[13px] text-destructive">{error}</p></div>
  if (!map || !map.root) return <div><TopologyViews /><p className="text-[13px] text-muted-foreground">Loading…</p></div>

  const allSlugs = collectSlugs(map.root)
  return (
    <div className="flex flex-col gap-4" data-testid="fleet-map">
      <TopologyViews />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-3xl text-[13px] text-foreground-secondary">
          Each workspace holds a lane per owner, with that owner&rsquo;s runners and agents. Select an agent to see
          and change which runners it runs on, and what it gets when it sends each other agent work; select a runner
          to configure it and see who depends on it.
        </p>
        <div className="flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground">
          <Legend />
          <button type="button" onClick={() => setCollapsed(new Set())} className="min-h-9 rounded-md border border-border px-2 hover:bg-muted">
            Expand all
          </button>
          <button
            type="button"
            onClick={() => setCollapsed(new Set(allSlugs.filter((s) => s !== map.root!.slug)))}
            className="min-h-9 rounded-md border border-border px-2 hover:bg-muted"
          >
            Collapse all
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-start gap-4">
        <MapCanvas
          map={map}
          collapsed={collapsed}
          selection={selection}
          onToggle={toggle}
          onSelect={select}
        />
        <SidePanel
          map={map}
          fleet={fleet}
          selection={selection}
          onRefresh={refresh}
          onRunnerChanged={(fresh) => {
            setFleet((prev) => new Map(prev).set(fresh.id, fresh))
            refresh()
          }}
          busy={busy}
          error={actionError}
          onSelect={select}
          onGrant={(e) => {
            const src = map.agents.get(e.source)
            const dst = map.agents.get(e.target)
            if (src?.loginUserId != null && dst) void act(() => grantAgentAdmin(e.target, src.loginUserId!, dst.workspace))
          }}
          onRevoke={(e) => {
            const src = map.agents.get(e.source)
            const dst = map.agents.get(e.target)
            if (src?.loginUserId != null && dst) void act(() => revokeAgentAdmin(e.target, src.loginUserId!, dst.workspace))
          }}
        />
      </div>
    </div>
  )
}

/** Enough agents in its own lanes that it reads better at full width. */
function isBusy(ws: MapWorkspace): boolean {
  return ws.lanes.reduce((n, l) => n + l.agents.length, 0) >= 4
}

function collectSlugs(ws: MapWorkspace): string[] {
  return [ws.slug, ...ws.children.flatMap(collectSlugs)]
}

function Legend(): JSX.Element {
  return (
    <span className="inline-flex flex-wrap items-center gap-3">
      <span className="inline-flex items-center gap-1.5"><span className="inline-block h-0.5 w-5 bg-success" />whole agent</span>
      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-5 border-t-2 border-dashed border-warning" />some capabilities</span>
    </span>
  )
}

// ---- the canvas -------------------------------------------------------------

interface Arrow {
  key: string
  d: string
  label: string
  labelX: number
  labelY: number
  tone: 'success' | 'warning'
}

const SVG = 'http://www.w3.org/2000/svg'

/** One arrow as SVG nodes. Tokens only, through classes; the label goes in as
 *  text, never markup, since it carries agent names. */
function drawArrow(a: Arrow): SVGGElement {
  const g = document.createElementNS(SVG, 'g')
  g.dataset.testid = `arrow-${a.key}`
  const path = document.createElementNS(SVG, 'path')
  path.setAttribute('d', a.d)
  path.setAttribute('fill', 'none')
  path.setAttribute('stroke-width', '2')
  path.setAttribute('opacity', '0.9')
  path.setAttribute('class', a.tone === 'success' ? 'stroke-success' : 'stroke-warning')
  path.setAttribute('marker-end', `url(#map-head-${a.tone})`)
  if (a.tone === 'warning') path.setAttribute('stroke-dasharray', '6 5')
  const text = document.createElementNS(SVG, 'text')
  text.setAttribute('x', String(a.labelX))
  text.setAttribute('y', String(a.labelY))
  text.setAttribute('text-anchor', 'middle')
  text.setAttribute('class', `text-[11px] font-medium ${a.tone === 'success' ? 'fill-success' : 'fill-warning'}`)
  text.setAttribute('stroke', 'var(--background)')
  text.setAttribute('stroke-width', '4')
  text.setAttribute('paint-order', 'stroke')
  text.textContent = a.label
  g.append(path, text)
  return g
}

function MapCanvas({
  map,
  collapsed,
  selection,
  onToggle,
  onSelect,
}: {
  map: FleetMap
  collapsed: Set<string>
  selection: Selection
  onToggle: (ws: string) => void
  onSelect: (s: Selection) => void
}): JSX.Element {
  const container = useRef<HTMLDivElement>(null)
  const nodes = useRef(new Map<string, HTMLElement>())
  const layer = useRef<SVGGElement>(null)
  const [tick, setTick] = useState(0)

  const register = useCallback((key: string) => (el: HTMLElement | null) => {
    if (el) nodes.current.set(key, el)
    else nodes.current.delete(key)
  }, [])

  // Re-measure when the box changes size (window, panel wrapping, fonts).
  useEffect(() => {
    const el = container.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setTick((t) => t + 1))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useLayoutEffect(() => {
    const root = container.current
    const g = layer.current
    if (!root || !g) return
    // Drawn straight into the SVG after layout rather than through state: the
    // geometry exists only once the boxes have been laid out, and a second
    // render to carry it would flash arrows from the previous layout.
    g.replaceChildren()
    if (selection?.kind !== 'agent' || !map.root) return
    const from = nodes.current.get(`agent:${selection.slug}`)
    if (!from) return
    const base = root.getBoundingClientRect()
    const rectOf = (el: HTMLElement) => {
      const r = el.getBoundingClientRect()
      return { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height }
    }
    const src = rectOf(from)
    // Several agents hidden in one collapsed box share one arrow onto it.
    const targets = new Map<string, { el: HTMLElement; tone: Arrow['tone']; names: string[]; label: string }>()
    for (const a of map.agents.values()) {
      if (a.slug === selection.slug) continue
      const e = map.edges.get(edgeKey(selection.slug, a.slug))
      if (!e || e.access === 'none') continue
      const hider = hiddenBy(map.root, a.workspace, collapsed)
      const key = hider ? `ws:${hider}` : `agent:${a.slug}`
      const el = nodes.current.get(key)
      if (!el) continue
      const tone: Arrow['tone'] = e.access === 'full' ? 'success' : 'warning'
      const prev = targets.get(key)
      if (prev) {
        prev.names.push(a.name)
        if (tone === 'warning') prev.tone = 'warning'
      } else {
        targets.set(key, { el, tone, names: [a.name], label: e.access === 'full' ? 'all' : `${e.capabilities.join(', ')} only` })
      }
    }
    const out: Arrow[] = []
    for (const [key, t] of targets) {
      const geometry = curve(src, rectOf(t.el))
      if (!geometry) continue
      // Onto a collapsed box: say how many agents inside it this one reaches.
      const label = key.startsWith('ws:') ? `${t.names.length} inside` : t.label
      out.push({ key, tone: t.tone, label, ...geometry })
    }
    g.replaceChildren(...out.map(drawArrow))
  }, [map, collapsed, selection, tick])

  const selectedAgent = selection?.kind === 'agent' ? map.agents.get(selection.slug) : undefined
  const selectedRunner = selection?.kind === 'runner' ? map.runners.get(selection.id) : undefined
  const adding = selection?.kind === 'add' ? addKey(selection.workspace, selection.owner) : null

  return (
    <div ref={container} className="relative min-w-0 flex-[999_1_640px]" data-testid="map-canvas">
      <WorkspaceBox
        ws={map.root!}
        map={map}
        collapsed={collapsed}
        selectedAgent={selectedAgent}
        selectedRunner={selectedRunner}
        adding={adding}
        orderSelected={selection?.kind === 'order' ? selection.workspace : null}
        onToggle={onToggle}
        onSelect={onSelect}
        register={register}
      />
      {map.outside.length > 0 && (
        <section className="mt-3 rounded-xl border border-dashed border-border p-3" data-testid="map-outside">
          <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Runners outside this tree that agents here use
          </h3>
          <div className="flex flex-wrap gap-2">
            {map.outside.map((r) => (
              <RunnerChip key={r.id} runner={r} selectedAgent={selectedAgent} selectedRunner={selectedRunner} onSelect={onSelect} register={register} />
            ))}
          </div>
        </section>
      )}
      <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible" aria-hidden="true">
        <defs>
          <marker id="map-head-success" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0 0 L8 4 L0 8 Z" className="fill-success" />
          </marker>
          <marker id="map-head-warning" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0 0 L8 4 L0 8 Z" className="fill-warning" />
          </marker>
        </defs>
        <g ref={layer} />
      </svg>
    </div>
  )
}

function WorkspaceBox({
  ws,
  map,
  collapsed,
  selectedAgent,
  selectedRunner,
  adding,
  orderSelected,
  onToggle,
  onSelect,
  register,
}: {
  ws: MapWorkspace
  map: FleetMap
  collapsed: Set<string>
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  adding: string | null
  orderSelected: string | null
  onToggle: (ws: string) => void
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  const isCollapsed = collapsed.has(ws.slug)
  const Chevron = isCollapsed ? ChevronRight : ChevronDown
  const empty = ws.agentCount === 0 && ws.runnerCount === 0
  return (
    <section
      ref={register(`ws:${ws.slug}`)}
      className={clsx(
        'rounded-xl border border-border',
        ws.depth % 2 === 0 ? 'bg-card' : 'bg-background',
      )}
      // A busy workspace takes the whole row beside its siblings: squeezed into
      // one grid column its agents stack, and the arrows between neighbours
      // shrink to stubs running over the card text.
      style={ws.depth > 0 && !isCollapsed && isBusy(ws) ? { gridColumn: '1 / -1' } : undefined}
      data-testid={`map-ws-${ws.slug}`}
    >
      <h3 className="m-0">
        <button
          type="button"
          onClick={() => onToggle(ws.slug)}
          aria-expanded={!isCollapsed}
          className="flex min-h-11 w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-xl px-3 py-2 text-left hover:bg-muted/50"
        >
          <Chevron className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className={clsx('font-semibold text-foreground', ws.depth === 0 ? 'text-[16px]' : 'text-[14px]')}>
            {ws.displayName}
          </span>
          <span className="text-[12px] text-muted-foreground">
            {empty
              ? 'no agents or runners'
              : `${ws.agentCount} agent${ws.agentCount === 1 ? '' : 's'} · ${ws.runnerCount} runner${ws.runnerCount === 1 ? '' : 's'}`}
          </span>
          {ws.problemCount > 0 && (
            <span className="rounded border border-destructive/30 bg-destructive/10 px-1.5 text-[11px] text-destructive">
              {ws.problemCount} can&rsquo;t run
            </span>
          )}
        </button>
      </h3>
      {!isCollapsed && (
        <DefaultOrderLine ws={ws} map={map} selected={orderSelected === ws.slug} onSelect={onSelect} />
      )}
      {!isCollapsed && !empty && (
        <div className="flex flex-col gap-3 px-3 pb-3">
          {ws.lanes.length > 0 && (
            <div className="flex flex-wrap gap-3">
              {ws.lanes.map((lane) => (
                <OwnerLane
                  key={lane.owner ?? '∅'}
                  workspace={ws.slug}
                  lane={lane}
                  map={map}
                  selectedAgent={selectedAgent}
                  selectedRunner={selectedRunner}
                  adding={adding === addKey(ws.slug, lane.owner ?? '')}
                  onSelect={onSelect}
                  register={register}
                />
              ))}
            </div>
          )}
          {ws.children.length > 0 && (
            <div className="grid items-start gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(320px, 100%), 1fr))' }}>
              {ws.children.map((c) => (
                <WorkspaceBox
                  key={c.slug}
                  ws={c}
                  map={map}
                  collapsed={collapsed}
                  selectedAgent={selectedAgent}
                  selectedRunner={selectedRunner}
                  adding={adding}
                  orderSelected={orderSelected}
                  onToggle={onToggle}
                  onSelect={onSelect}
                  register={register}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function addKey(workspace: string, owner: string): string {
  return `${workspace}\u0000${owner}`
}

/** The workspace's default runners, one line, at the top of its box — the
 *  order every agent here without its own follows. Selecting it opens the editor. */
function DefaultOrderLine({
  ws,
  map,
  selected,
  onSelect,
}: {
  ws: MapWorkspace
  map: FleetMap
  selected: boolean
  onSelect: (s: Selection) => void
}): JSX.Element {
  const from = map.orderFrom.get(ws.slug) ?? null
  const names = ws.order.map((id) => shortRunner(map.runners.get(id)?.name ?? '?'))
  return (
    <div className="px-3 pb-2">
      <button
        type="button"
        onClick={() => onSelect(selected ? null : { kind: 'order', workspace: ws.slug })}
        aria-pressed={selected}
        className={clsx(
          'inline-flex min-h-8 flex-wrap items-center gap-1.5 rounded-md border px-2 py-1 text-left text-[12px]',
          selected ? 'border-primary bg-primary/10' : 'border-dashed border-border hover:bg-muted',
        )}
        data-testid={`map-order-${ws.slug}`}
      >
        <span className="text-muted-foreground">Default runners:</span>
        {names.length > 0 ? (
          <span className="font-mono text-foreground">{names.join(' → ')}</span>
        ) : from ? (
          <span className="text-foreground-secondary">follows {from}&rsquo;s</span>
        ) : (
          <span className="text-warning">none set</span>
        )}
      </button>
    </div>
  )
}

/** Grow by agent count, and ask for room for up to four cards side by side
 *  (each 200px + gap) before wrapping onto a row of its own. */
function laneFlex(lane: Lane): string {
  const cards = Math.max(1, Math.min(lane.agents.length, 4))
  return `${cards} 1 ${cards * 208 + 22}px`
}

function OwnerLane({
  workspace,
  lane,
  map,
  selectedAgent,
  selectedRunner,
  adding,
  onSelect,
  register,
}: {
  workspace: string
  lane: Lane
  map: FleetMap
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  adding: boolean
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  return (
    <div
      className={clsx(
        'flex min-w-0 flex-col gap-2 rounded-lg border p-2.5',
        lane.owner ? 'border-border bg-muted/30' : 'border-dashed border-border',
      )}
      // Width in proportion to what the lane holds: equal shares put a
      // five-agent lane beside two near-empty ones and stacked its cards.
      style={{ flex: laneFlex(lane) }}
      data-testid={`lane-${lane.owner ?? 'none'}`}
    >
      <div className="flex items-center gap-2 text-[12px]">
        <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[10px] font-semibold text-primary">
          {initials(lane.owner)}
        </span>
        <span className="truncate text-foreground-secondary">{lane.owner ?? 'No owner set'}</span>
      </div>
      {(lane.runners.length > 0 || lane.owner) && (
        <div className="flex flex-wrap gap-1.5">
          {lane.runners.map((r) => (
            <RunnerChip key={r.id} runner={r} selectedAgent={selectedAgent} selectedRunner={selectedRunner} onSelect={onSelect} register={register} />
          ))}
          {/* A runner always has an owner, so the "No owner set" lane has no
              one to add a box for. */}
          {lane.owner && (
            <button
              type="button"
              onClick={() => onSelect(adding ? null : { kind: 'add', workspace, owner: lane.owner! })}
              aria-pressed={adding}
              className={clsx(
                'inline-flex min-h-8 items-center gap-1 rounded-md border border-dashed px-2 py-1 text-[12px]',
                adding ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
              data-testid={`map-add-runner-${workspace}-${lane.owner}`}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              Add a runner
            </button>
          )}
        </div>
      )}
      {lane.agents.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {lane.agents.map((a) => (
            <AgentCard key={a.slug} agent={a} map={map} selectedAgent={selectedAgent} selectedRunner={selectedRunner} onSelect={onSelect} register={register} />
          ))}
        </div>
      )}
    </div>
  )
}

function RunnerChip({
  runner,
  selectedAgent,
  selectedRunner,
  onSelect,
  register,
}: {
  runner: TopologyRunnerOut
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  const isSelected = selectedRunner?.id === runner.id
  // With an agent selected: this runner's place in that agent's order, if any.
  const rank = selectedAgent?.routing
    ? defaultRoutes(selectedAgent.routing).findIndex((r) => r.runner_id === runner.id)
    : -1
  const route = rank >= 0 ? defaultRoutes(selectedAgent!.routing!)[rank] : undefined
  const ruled = selectedAgent?.routing ? ruleRoutes(selectedAgent.routing).some((r) => r.runner_id === runner.id) : false
  const dimmed = (selectedAgent && rank < 0 && !ruled) || (selectedRunner && !isSelected)
  return (
    <button
      ref={register(`runner:${runner.id}`)}
      type="button"
      onClick={() => onSelect(isSelected ? null : { kind: 'runner', id: runner.id })}
      aria-pressed={isSelected}
      title={`${runner.name} · ${runner.kind} · ${runner.status}${runner.host ? ` · ${runner.host}` : ''}`}
      className={clsx(
        'inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2 py-1 text-[12px] transition-opacity',
        isSelected || rank >= 0 || ruled ? 'border-primary bg-primary/10' : 'border-border bg-card hover:bg-muted',
        dimmed && 'opacity-40',
        route && !route.enabled && 'line-through',
      )}
      data-testid={`map-runner-${runner.name}`}
    >
      {rank >= 0 && <span className="font-semibold text-primary">{rank + 1}</span>}
      <span className={statusTone(runner.status)} aria-hidden="true">●</span>
      <span className="font-mono text-foreground">{shortRunner(runner.name)}</span>
      <span className="text-muted-foreground">
        {runner.status === 'online' ? runner.kind : runner.status}
        {runner.agent_count === 0 ? ' · idle' : ''}
      </span>
    </button>
  )
}

function AgentCard({
  agent,
  map,
  selectedAgent,
  selectedRunner,
  onSelect,
  register,
}: {
  agent: MapAgent
  map: FleetMap
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  const isSelected = selectedAgent?.slug === agent.slug
  const edge = selectedAgent && !isSelected ? map.edges.get(edgeKey(selectedAgent.slug, agent.slug)) : undefined
  const usesRunner = selectedRunner && agent.routing?.routes.some((r) => r.runner_id === selectedRunner.id)
  const sole = selectedRunner && agent.routing ? dependsSolelyOn(agent.routing, selectedRunner.id, map.runners) : false
  const dimmed = (selectedAgent && !isSelected && (!edge || edge.access === 'none')) || (selectedRunner && !usesRunner)
  const order = agent.routing ? defaultRoutes(agent.routing).filter((r) => r.enabled) : []
  const rules = agent.routing ? ruleRoutes(agent.routing).length : 0
  const health = HEALTH[agent.health]
  return (
    <button
      ref={register(`agent:${agent.slug}`)}
      type="button"
      onClick={() => onSelect(isSelected ? null : { kind: 'agent', slug: agent.slug })}
      aria-pressed={isSelected}
      className={clsx(
        'flex min-h-11 w-[200px] max-w-full flex-col items-start gap-0.5 rounded-lg border px-2.5 py-2 text-left transition-opacity',
        isSelected && 'border-primary bg-primary/10 ring-2 ring-primary/30',
        !isSelected && edge?.access === 'full' && 'border-success bg-card',
        !isSelected && edge?.access === 'confined' && 'border-warning bg-card',
        !isSelected && !edge && (sole ? 'border-destructive bg-card' : usesRunner ? 'border-primary bg-card' : 'border-border bg-card hover:bg-muted'),
        dimmed && 'opacity-40',
      )}
      data-testid={`map-agent-${agent.slug}`}
    >
      <span className="flex w-full items-baseline justify-between gap-2">
        <span className="text-[13px] font-semibold text-foreground">{agent.name}</span>
        <span className="text-[11px] text-muted-foreground">{agent.turnMode}</span>
      </span>
      <span className="text-[11px] text-muted-foreground">
        {order.length
          ? `runs on ${order.map((r) => shortRunner(map.runners.get(r.runner_id)?.name ?? '?')).join(' → ')}`
          : 'no runner'}
        {rules ? ` + ${rules} rule${rules === 1 ? '' : 's'}` : ''}
        {agent.follows ? ` · ${agent.follows.workspace} default` : ''}
      </span>
      {(agent.follows?.missing_repo ?? []).length > 0 && (
        <span className="text-[11px] text-warning">
          {(agent.follows?.missing_repo ?? []).map((id) => shortRunner(map.runners.get(id)?.name ?? '?')).join(', ')} lacks its repo
        </span>
      )}
      {agent.interfacePublished && <span className="text-[11px] text-info">interface published</span>}
      {health && <span className={clsx('text-[11px]', health.className)}>{health.text}</span>}
      {sole && <span className="text-[11px] font-semibold text-destructive">stops if it goes dark</span>}
    </button>
  )
}

// ---- the side panel ----------------------------------------------------------

function SidePanel({
  map,
  fleet,
  selection,
  onRefresh,
  onRunnerChanged,
  busy,
  error,
  onSelect,
  onGrant,
  onRevoke,
}: {
  map: FleetMap
  fleet: Map<string, RunnerOut>
  selection: Selection
  onRefresh: () => void
  onRunnerChanged: (r: RunnerOut) => void
  busy: boolean
  error: string | null
  onSelect: (s: Selection) => void
  onGrant: (e: AgentEdgeOut) => void
  onRevoke: (e: AgentEdgeOut) => void
}): JSX.Element {
  return (
    <aside
      className={clsx(
        'flex min-w-0 flex-[1_1_300px] flex-col gap-4 rounded-xl border border-border bg-card p-4 text-[13px]',
        // Room for the routing table and the runner's controls once there is one.
        selection?.kind === 'agent' ? 'lg:max-w-lg' : selection ? 'lg:max-w-md' : 'lg:max-w-sm',
      )}
      data-testid="map-panel"
    >
      {selection?.kind === 'agent' && map.agents.get(selection.slug) ? (
        <AgentPanel
          agent={map.agents.get(selection.slug)!}
          map={map}
          busy={busy}
          onSelect={onSelect}
          onGrant={onGrant}
          onRevoke={onRevoke}
          onRoutingSaved={onRefresh}
        />
      ) : selection?.kind === 'runner' && map.runners.get(selection.id) ? (
        <RunnerPanel
          runner={map.runners.get(selection.id)!}
          detail={fleet.get(selection.id)}
          map={map}
          onSelect={onSelect}
          onChanged={onRunnerChanged}
          onRetired={() => {
            onSelect(null)
            onRefresh()
          }}
        />
      ) : selection?.kind === 'order' ? (
        <WorkspaceOrderPanel
          key={selection.workspace}
          workspace={selection.workspace}
          map={map}
          fleet={fleet}
          onSaved={onRefresh}
          onSelectAgent={(slug) => onSelect({ kind: 'agent', slug })}
        />
      ) : selection?.kind === 'add' ? (
        <AddRunnerPanel workspace={selection.workspace} owner={selection.owner} map={map} />
      ) : (
        <Summary map={map} onSelect={onSelect} />
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </aside>
  )
}

function PanelHeading({ children }: { children: React.ReactNode }): JSX.Element {
  return <h4 className="m-0 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{children}</h4>
}

function AgentPanel({
  agent,
  map,
  busy,
  onSelect,
  onGrant,
  onRevoke,
  onRoutingSaved,
}: {
  agent: MapAgent
  map: FleetMap
  busy: boolean
  onSelect: (s: Selection) => void
  onGrant: (e: AgentEdgeOut) => void
  onRevoke: (e: AgentEdgeOut) => void
  onRoutingSaved: () => void
}): JSX.Element {
  const others = [...map.agents.values()].filter((a) => a.slug !== agent.slug)
  const rank = { full: 0, confined: 1, none: 2 } as const
  const outgoing = others
    .map((a) => ({ a, e: map.edges.get(edgeKey(agent.slug, a.slug)) }))
    .filter((x): x is { a: MapAgent; e: AgentEdgeOut } => Boolean(x.e))
    .sort((x, y) => rank[x.e.access] - rank[y.e.access] || x.a.name.localeCompare(y.a.name))
  const incoming = others
    .map((a) => ({ a, e: map.edges.get(edgeKey(a.slug, agent.slug)) }))
    .filter((x): x is { a: MapAgent; e: AgentEdgeOut } => Boolean(x.e) && x.e!.basis !== 'no-login')
  const src = map.profiles.get(agent.slug)
  return (
    <>
      <div className="flex flex-col gap-1">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="m-0 text-[18px] font-semibold text-foreground">{agent.name}</h3>
          <Link to={`/w/${agent.workspace}/agents/${agent.slug}/settings`} className="text-[12px] text-primary hover:underline">
            Settings →
          </Link>
        </div>
        <span className="text-[12px] text-muted-foreground">
          {agent.workspace} · {agent.ownerEmail ? `owner ${agent.ownerEmail}` : 'no owner set'}
        </span>
        <span className="text-[12px] text-muted-foreground">
          {agent.loginEmail ? `signs in as ${agent.loginEmail}` : 'no canopy login — it cannot send other agents work'}
        </span>
      </div>

      {/* The agent's own routing table, the one on its Settings page — edited
          here, redrawn on the map (its runner chips number themselves). */}
      <section className="flex flex-col gap-1.5" data-testid="map-agent-routing">
        <PanelHeading>Where its work runs</PanelHeading>
        {agent.health !== 'ok' && (
          <span className="text-[12px] text-destructive">
            {agent.health === 'unrouted' ? 'No runner: its turns cannot run.' : 'None of its runners is live.'}
          </span>
        )}
        <AgentRouting
          key={agent.slug}
          agentSlug={agent.slug}
          agentName={agent.name}
          workspace={agent.workspace}
          initialTurnMode={(agent.turnMode || 'manual') as TurnMode}
          onSaved={onRoutingSaved}
        />
      </section>

      <section className="flex flex-col gap-1.5">
        <PanelHeading>When {agent.name} sends work to…</PanelHeading>
        {!agent.loginEmail && <span className="text-muted-foreground">Nobody: it has no login to send with.</span>}
        {agent.loginEmail &&
          outgoing.map(({ a, e }) => {
            const dst = map.profiles.get(a.slug)
            return (
              <div
                key={a.slug}
                className={clsx(
                  'flex flex-col gap-1.5 rounded-md px-1 py-1',
                  e.access === 'confined' && 'border border-warning/30 bg-warning/10 p-2',
                )}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <button type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className="font-medium text-foreground hover:underline">
                    {a.name}
                  </button>
                  <span className={clsx('text-[12px]', e.access === 'full' ? 'text-success' : e.access === 'confined' ? 'text-warning' : 'text-muted-foreground')}>
                    {e.access === 'full' ? 'All' : e.access === 'confined' ? e.capabilities.join(', ') + ' only' : 'None'}
                  </span>
                </div>
                {e.access !== 'full' && src && dst && (
                  <span className="text-[12px] text-foreground-secondary">{explainEdge(e, src, dst)}</span>
                )}
                {e.can_grant && e.access !== 'full' && (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onGrant(e)}
                      className="min-h-9 self-start rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
                      data-testid={`map-grant-${a.slug}`}
                    >
                      Make {agent.name} an admin of {a.name}
                    </button>
                    <span className="text-[11px] text-muted-foreground">
                      Then {agent.fullPeople.length} {agent.fullPeople.length === 1 ? 'person' : 'people'} who get all of {agent.name} can steer {a.name} through it.
                    </span>
                  </>
                )}
                {e.can_revoke && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onRevoke(e)}
                    className="self-start text-[12px] text-destructive hover:underline disabled:opacity-40"
                  >
                    Revoke admin
                  </button>
                )}
              </div>
            )
          })}
      </section>

      <section className="flex flex-col gap-1">
        <PanelHeading>Who can send {agent.name} work</PanelHeading>
        {incoming.length === 0 && <span className="text-muted-foreground">No other agent with a login.</span>}
        {incoming.map(({ a, e }) => (
          <div key={a.slug} className="flex items-baseline justify-between gap-2">
            <button type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className="text-foreground-secondary hover:underline">
              {a.name}
            </button>
            <span className={clsx('text-[12px]', e.access === 'full' ? 'text-success' : e.access === 'confined' ? 'text-warning' : 'text-muted-foreground')}>
              {e.access === 'full' ? 'All' : e.access === 'confined' ? e.capabilities.join(', ') + ' only' : e.basis === 'not-member' ? `None · not in ${agent.workspace}` : 'None'}
            </span>
          </div>
        ))}
      </section>
    </>
  )
}

function RunnerPanel({
  runner,
  detail,
  map,
  onSelect,
  onChanged,
  onRetired,
}: {
  runner: TopologyRunnerOut
  /** The same runner as GET /harness/runners/ serves it — absent when the
   *  viewer cannot list it (a box homed outside their workspaces). */
  detail: RunnerOut | undefined
  map: FleetMap
  onSelect: (s: Selection) => void
  onChanged: (r: RunnerOut) => void
  onRetired: () => void
}): JSX.Element {
  const users = [...map.agents.values()].filter((a) => a.routing?.routes.some((r) => r.runner_id === runner.id))
  const liveLink = (
    <Link to={`/supervisor?tab=runners&runner=${runner.id}`} className="text-[12px] text-primary hover:underline">
      Live status →
    </Link>
  )
  return (
    <>
      {detail ? (
        <div className="flex flex-col gap-2">
          <div className="flex justify-end">{liveLink}</div>
          {/* The supervisor's own runner detail: login, admins, flags, drills,
              pause and retire, each shown only to whoever the server says may
              use it. Routing is left to the agents, below and on the map. */}
          <RunnerDetail
            key={detail.id}
            runner={detail}
            onChanged={onChanged}
            onRetired={onRetired}
            agentWorkspace={(slug) => map.agents.get(slug)?.workspace}
          />
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="m-0 font-mono text-[16px] font-semibold text-foreground">{runner.name}</h3>
            {liveLink}
          </div>
          <span className={clsx('text-[12px]', statusTone(runner.status))}>
            ● {runner.status}
            {!runner.ready && <span className="text-warning"> · not ready</span>}
          </span>
          <span className="text-[12px] text-muted-foreground">
            {runner.kind}
            {runner.host ? ` · ${runner.host}` : ''} · lives in {runner.workspace ?? '—'} · owned by {runner.owner_email ?? '—'}
          </span>
          <span className="text-[12px] text-muted-foreground" data-testid="map-runner-not-listed">
            You cannot configure this runner: it is not in a workspace you belong to. {runner.owner_email ?? 'Its owner'} can.
          </span>
        </div>
      )}
      <section className="flex flex-col gap-1" data-testid="map-runner-users">
        <PanelHeading>Agents that route to it</PanelHeading>
        {users.length === 0 && <span className="text-muted-foreground">None: this runner serves nobody.</span>}
        {users.map((a) => {
          const sole = a.routing ? dependsSolelyOn(a.routing, runner.id, map.runners) : false
          const pos = a.routing ? defaultRoutes(a.routing).findIndex((r) => r.runner_id === runner.id) : -1
          return (
            <div key={a.slug} className="flex items-baseline justify-between gap-2">
              <button type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className={clsx('hover:underline', sole ? 'font-semibold text-destructive' : 'text-foreground-secondary')}>
                {a.name}
              </button>
              <span className="text-[12px] text-muted-foreground">
                {pos >= 0 ? `#${pos + 1} in order` : 'by a rule'}
                {sole ? ' · stops if it goes dark' : ''}
              </span>
            </div>
          )
        })}
        {users.length > 0 && (
          <span className="text-[11px] text-muted-foreground">Select an agent to change where it runs.</span>
        )}
      </section>
    </>
  )
}

function AddRunnerPanel({ workspace, owner, map }: { workspace: string; owner: string; map: FleetMap }): JSX.Element {
  const auth = useAuth()
  const me = auth.user?.email ?? null
  const isMe = me !== null && me === owner
  const base = `${window.location.origin}${import.meta.env.BASE_URL}`
  const command = pairingCommand(workspace, base)
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(command).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
  const wsName = findWorkspace(map.root, workspace)?.displayName ?? workspace
  return (
    <div className="flex flex-col gap-3" data-testid="map-add-runner-panel">
      <div className="flex flex-col gap-1">
        <h3 className="m-0 text-[16px] font-semibold text-foreground">Add a runner</h3>
        <span className="text-[12px] text-muted-foreground">
          {wsName} · {isMe ? 'your lane' : `${owner}'s lane`}
        </span>
      </div>
      <p className="m-0 text-[13px] text-foreground-secondary">
        A runner is paired from the box itself, one per macOS account. Run this in a terminal on that account:
      </p>
      <div className="flex items-start gap-2 rounded-md border border-input bg-input p-2">
        <code className="min-w-0 flex-1 break-all font-mono text-[12px] text-foreground" data-testid="map-pairing-command">
          {command}
        </code>
        <button
          type="button"
          onClick={copy}
          className="inline-flex min-h-8 shrink-0 items-center gap-1 rounded-md border border-border px-2 text-[12px] text-foreground hover:bg-muted"
          data-testid="map-pairing-copy"
        >
          {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <section className="flex flex-col gap-1">
        <PanelHeading>Before you run it</PanelHeading>
        <ul className="m-0 flex list-disc flex-col gap-1 pl-4 text-[12px] text-foreground-secondary">
          <li>
            canopy-web cloned at <code className="font-mono">~/emdash-projects/canopy-web</code>, and{' '}
            <code className="font-mono">uv</code> installed. The script fetches and installs from{' '}
            <code className="font-mono">main</code> itself.
          </li>
          <li>
            A canopy token at <code className="font-mono">~/.claude/canopy/workbench-token</code> (the{' '}
            <code className="font-mono">canopy:canopy-web-pat-mint</code> skill writes it).{' '}
            {isMe
              ? 'The box pairs as whoever that token belongs to, so use yours to land it here.'
              : `The box pairs as whoever that token belongs to, so it lands in ${owner}'s lane only if it is theirs.`}
          </li>
          <li>
            Afterwards, relaunch emdash with the <span className="text-foreground">Emdash CDP</span> app it builds. That
            quits emdash first, so every session open in it stops.
          </li>
        </ul>
      </section>
      <p className="m-0 text-[12px] text-muted-foreground">
        It appears here once it heartbeats. Then select it to set its login and admins, and add it to an agent&rsquo;s
        runners. A cloud runner is stood up with <code className="font-mono">runner/ec2/up.sh</code> and{' '}
        <code className="font-mono">wire.sh</code> instead, which need AWS access.
      </p>
    </div>
  )
}

function findWorkspace(ws: MapWorkspace | null, slug: string): MapWorkspace | null {
  if (!ws) return null
  if (ws.slug === slug) return ws
  for (const c of ws.children) {
    const hit = findWorkspace(c, slug)
    if (hit) return hit
  }
  return null
}

function Summary({ map, onSelect }: { map: FleetMap; onSelect: (s: Selection) => void }): JSX.Element {
  const agents = [...map.agents.values()]
  const stuck = agents.filter((a) => a.health !== 'ok')
  const confined = [...map.edges.values()].filter((e) => e.access === 'confined')
  const idle = [...map.runners.values()].filter((r) => r.agent_count === 0)
  return (
    <>
      <h3 className="m-0 text-[15px] font-semibold text-foreground">What needs attention</h3>
      <section className="flex flex-col gap-1">
        <PanelHeading>Agents whose turns can&rsquo;t run</PanelHeading>
        {stuck.length === 0 && <span className="text-success">None.</span>}
        {stuck.map((a) => (
          <button key={a.slug} type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className="flex justify-between gap-2 text-left hover:underline">
            <span>{a.name}</span>
            <span className="text-[12px] text-destructive">{HEALTH[a.health]?.text}</span>
          </button>
        ))}
      </section>
      <section className="flex flex-col gap-1">
        <PanelHeading>Agents held to some capabilities by another</PanelHeading>
        {confined.length === 0 && <span className="text-success">None.</span>}
        {confined.map((e) => (
          <button key={edgeKey(e.source, e.target)} type="button" onClick={() => onSelect({ kind: 'agent', slug: e.source })} className="flex justify-between gap-2 text-left hover:underline">
            <span>{map.agents.get(e.source)?.name} → {map.agents.get(e.target)?.name}</span>
            <span className="text-[12px] text-warning">{e.capabilities.join(', ')} only</span>
          </button>
        ))}
      </section>
      {idle.length > 0 && (
        <section className="flex flex-col gap-1">
          <PanelHeading>Runners serving nobody</PanelHeading>
          {idle.map((r) => (
            <button key={r.id} type="button" onClick={() => onSelect({ kind: 'runner', id: r.id })} className="text-left font-mono hover:underline">
              {r.name}
            </button>
          ))}
        </section>
      )}
    </>
  )
}
