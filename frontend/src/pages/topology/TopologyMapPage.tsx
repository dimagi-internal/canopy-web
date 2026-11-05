import { useCallback, useEffect, useState, type JSX } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { grantAgentAdmin, revokeAgentAdmin } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { getAgentTopology, getRunnerTopology } from '@/api/workspaces'
import { TopologyViews } from '@/pages/topology/TopologyViews'
import { buildFleetMap, initiallyCollapsed, type FleetMap, type MapWorkspace } from '@/pages/topology/topologyMap'
import { ApiError } from '@/api/problem'
import { MapCanvas } from '@/pages/topology/map/MapCanvas'
import { type Selection } from '@/pages/topology/map/shared'
import { SidePanel } from '@/pages/topology/panels/SidePanel'

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
        if (e instanceof ApiError && (e.status === 403 || e.status === 404)) setForbidden(true)
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
          workspace={slug}
          map={map}
          fleet={fleet}
          selection={selection}
          onRefresh={refresh}
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

function collectSlugs(ws: MapWorkspace): string[] {
  return [ws.slug, ...ws.children.flatMap(collectSlugs)]
}

function Legend(): JSX.Element {
  return (
    <span className="inline-flex flex-wrap items-center gap-3">
      <span className="inline-flex items-center gap-1.5"><span className="inline-block h-0.5 w-5 bg-success" />whole agent</span>
      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-5 border-t-2 border-dashed border-warning" />read mode or connected sites</span>
    </span>
  )
}

// ---- the canvas -------------------------------------------------------------

