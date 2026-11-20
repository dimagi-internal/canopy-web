import { lazy, Suspense, useMemo, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'

import { getWorkspaceGraph, type GraphEdgeOut, type GraphNodeOut, type WorkspaceGraphOut } from '@/api/workspaces'
import { Td, Th, Tr } from '@/components/DataTable'
import { useLoad } from '@/hooks/useLoad'
import { relativeAge } from '@/lib/relativeAge'
import {
  adjacency, applyFilters, defaultFilters, describeEdge, EDGE_KINDS, EDGE_STYLE, hrefOf, neighbourhood, NODE_STYLE,
  NODE_TYPES, present, searchNodes, TIER_STYLE, TIERS, type EdgeKind, type Filters, type NodeType, type Tier,
} from './relationGraph'

// RELATIONS: how people, agents, sessions, projects, tasks and learning outputs
// relate, in 3-D (`GET /api/workspaces/{slug}/graph`, any member). Everything in it
// is something the viewer already sees elsewhere; the server decides that. The 3-D
// canvas (three.js) is a lazy chunk, and a browser without WebGL gets a table.

const RelationsCanvas3D = lazy(() => import('./RelationsCanvas3D'))
const RANGES = [7, 30, 90] as const
const HEIGHT = 640

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl'))
  } catch {
    return false
  }
}

export function RelationsView({ slug }: { slug: string }): JSX.Element {
  const [days, setDays] = useState<number>(30)
  const [tree, setTree] = useState(true)
  const [filters, setFilters] = useState<Filters>(defaultFilters)
  const { data, error } = useLoad(
    () => getWorkspaceGraph(slug, { days, tree, proposed: filters.proposed }),
    [slug, days, tree, filters.proposed], { keepData: true })

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
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
      </div>
      {error ? <p className="text-[13px] text-destructive">{error.message}</p>
        : !data ? <p className="text-[13px] text-muted-foreground">Loading…</p>
          : <Relations data={data} here={slug} filters={filters} setFilters={setFilters} />}
    </div>
  )
}

function Relations({ data, here, filters, setFilters }: {
  data: WorkspaceGraphOut; here: string; filters: Filters; setFilters: (f: Filters) => void
}): JSX.Element {
  const [selected, setSelected] = useState<string | null>(null)
  const [depth, setDepth] = useState(1)
  const view = useMemo(() => applyFilters(data, filters), [data, filters])
  const adj = useMemo(() => adjacency(view), [view])
  const shown = selected && adj.has(selected) ? selected : null
  const lit = useMemo(() => (shown ? neighbourhood(adj, shown, depth) : null), [adj, shown, depth])
  const byId = useMemo(() => new Map(data.nodes.map((n) => [n.id, n])), [data])
  const webgl = useMemo(() => hasWebGL(), [])

  return (
    <div className="space-y-3">
      <Totals data={data} />
      {data.notes.length > 0 && <p className="text-[12px] text-muted-foreground">{data.notes.join(' ')}</p>}
      <div className="grid gap-3 lg:grid-cols-[210px_minmax(0,1fr)_300px]">
        <FilterPanel data={data} filters={filters} setFilters={setFilters} />
        <div className="min-w-0 space-y-2">
          <Search nodes={view.nodes} onPick={setSelected} />
          {view.nodes.length === 0 ? (
            <p className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground">
              Nothing to show with these filters in this window.
            </p>
          ) : webgl ? (
            <Suspense fallback={<p className="text-[13px] text-muted-foreground" style={{ height: HEIGHT }}>Loading the 3-D view…</p>}>
              <RelationsCanvas3D view={view} selected={shown} lit={lit} onSelect={setSelected} height={HEIGHT} />
            </Suspense>
          ) : (
            <NoWebGL view={view} byId={byId} onSelect={setSelected} />
          )}
        </div>
        <Detail node={shown ? byId.get(shown) ?? null : null} edges={view.edges} byId={byId} here={here}
                depth={depth} setDepth={setDepth} onSelect={setSelected} />
      </div>
    </div>
  )
}

function Totals({ data }: { data: WorkspaceGraphOut }): JSX.Element {
  return (
    <section aria-label="Totals" className="flex flex-wrap gap-x-4 gap-y-1 rounded-lg border border-border bg-card px-3 py-2 text-[12px]">
      {NODE_TYPES.filter((t) => data.totals.nodes[t]).map((t) => (
        <span key={t} className="inline-flex items-center gap-1.5 text-muted-foreground">
          <span className="inline-block size-2 rounded-full" style={{ background: `var(${NODE_STYLE[t].token})` }} />
          {NODE_STYLE[t].label} <b className="tabular-nums text-foreground">{data.totals.nodes[t]}</b>
        </span>
      ))}
      <span className="text-muted-foreground">
        · {data.edges.length.toLocaleString()} connections
        {data.proposed && <> · {data.totals.edges.proposed ?? 0} proposed</>}
      </span>
    </section>
  )
}

function toggle<T>(set: Set<T>, v: T): Set<T> {
  const next = new Set(set)
  if (next.has(v)) next.delete(v)
  else next.add(v)
  return next
}

function FilterPanel({ data, filters, setFilters }: {
  data: WorkspaceGraphOut; filters: Filters; setFilters: (f: Filters) => void
}): JSX.Element {
  const there = useMemo(() => present(data), [data])
  const set = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch })
  return (
    <aside aria-label="Filters" className="space-y-3 rounded-lg border border-border bg-card p-3 text-[12px]">
      <Group title="Show">
        {NODE_TYPES.filter((t) => there.types.has(t)).map((t: NodeType) => (
          <Check key={t} on={filters.types.has(t)} onChange={() => set({ types: toggle(filters.types, t) })}
                 swatch={NODE_STYLE[t].token} label={NODE_STYLE[t].label} />
        ))}
      </Group>
      <Group title="Connections">
        {EDGE_KINDS.filter((k) => there.kinds.has(k)).map((k: EdgeKind) => (
          <Check key={k} on={filters.kinds.has(k)} onChange={() => set({ kinds: toggle(filters.kinds, k) })}
                 swatch={EDGE_STYLE[k].token} label={EDGE_STYLE[k].label} line />
        ))}
      </Group>
      {there.kinds.has('linked') && (
        <Group title="Project ties">
          {TIERS.filter((t) => there.tiers.has(t) || t !== 'rejected').map((t: Tier) => (
            <Check key={t} on={filters.tiers.has(t)} onChange={() => set({ tiers: toggle(filters.tiers, t) })}
                   label={TIER_STYLE[t].label} />
          ))}
        </Group>
      )}
      <Group title="Learning">
        <Check on={filters.proposed} onChange={() => set({ proposed: !filters.proposed })}
               label="Ties canopy would make" hint="Computed now from each session's activity, stored nowhere. Curved lines with moving dots." />
      </Group>
      {there.workspaces.length > 1 && (
        <Group title="Workspace">
          <select value={filters.workspace} onChange={(e) => set({ workspace: e.target.value })}
                  className="min-h-8 w-full rounded-md border border-border bg-card px-1.5 text-foreground">
            <option value="">All of them</option>
            {there.workspaces.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </Group>
      )}
      <Check on={!filters.hideIsolated} onChange={() => set({ hideIsolated: !filters.hideIsolated })}
             label="Show unconnected" />
    </aside>
  )
}

function Group({ title, children }: { title: string; children: React.ReactNode }): JSX.Element {
  return (
    <fieldset className="space-y-0.5">
      <legend className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{title}</legend>
      {children}
    </fieldset>
  )
}

function Check({ on, onChange, label, swatch, line, hint }: {
  on: boolean; onChange: () => void; label: string; swatch?: string; line?: boolean; hint?: string
}): JSX.Element {
  return (
    <label className="flex min-h-7 items-center gap-1.5 text-foreground-secondary" title={hint}>
      <input type="checkbox" checked={on} onChange={onChange} />
      {swatch && <span className={line ? 'inline-block h-0.5 w-3' : 'inline-block size-2 rounded-full'} style={{ background: `var(${swatch})` }} />}
      <span className="truncate">{label}</span>
    </label>
  )
}

function Search({ nodes, onPick }: { nodes: GraphNodeOut[]; onPick: (id: string) => void }): JSX.Element {
  const [q, setQ] = useState('')
  const hits = useMemo(() => searchNodes(nodes, q), [nodes, q])
  return (
    <div className="relative">
      <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a person, agent, session, project or task"
             aria-label="Find a node"
             onKeyDown={(e) => { if (e.key === 'Enter' && hits[0]) { onPick(hits[0].id); setQ('') } }}
             className="min-h-9 w-full rounded-md border border-border bg-card px-2.5 text-[13px] text-foreground placeholder:text-placeholder" />
      {hits.length > 0 && (
        <ul className="absolute z-20 mt-1 w-full overflow-hidden rounded-md border border-border bg-popover text-[13px] text-popover-foreground shadow" role="listbox">
          {hits.map((n) => (
            <li key={n.id}>
              <button type="button" onClick={() => { onPick(n.id); setQ('') }}
                      className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-muted">
                <span className="inline-block size-2 shrink-0 rounded-full" style={{ background: `var(${NODE_STYLE[n.type].token})` }} />
                <span className="truncate">{n.label}</span>
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{n.type}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const OPEN: Partial<Record<NodeType, string>> = {
  person: 'Open member', agent: 'Open agent', session: 'Open session', project: 'Open project', task: 'Open task',
  workspace: 'Open its network',
}

function Detail({ node, edges, byId, here, depth, setDepth, onSelect }: {
  node: GraphNodeOut | null; edges: GraphEdgeOut[]; byId: Map<string, GraphNodeOut>; here: string
  depth: number; setDepth: (d: number) => void; onSelect: (id: string | null) => void
}): JSX.Element {
  const rows = useMemo(() => {
    if (!node) return []
    return edges.filter((e) => e.source === node.id || e.target === node.id)
      .map((e) => ({ e, other: byId.get(e.source === node.id ? e.target : e.source) }))
      .filter((r): r is { e: GraphEdgeOut; other: GraphNodeOut } => !!r.other)
      .sort((a, b) => EDGE_KINDS.indexOf(a.e.kind) - EDGE_KINDS.indexOf(b.e.kind) || b.e.weight - a.e.weight)
  }, [node, edges, byId])
  if (!node) {
    return (
      <aside className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground">
        Click a shape, or find one, to see what it is connected to and open its page.
      </aside>
    )
  }
  const href = hrefOf(node, here)
  return (
    <aside className="min-w-0 overflow-y-auto rounded-lg border border-border bg-card p-3 text-[13px]" style={{ maxHeight: HEIGHT + 50 }} aria-label="Selected">
      <div className="mb-1 flex items-start justify-between gap-2">
        <h2 className="line-clamp-3 font-semibold text-foreground" title={node.label}>{node.label}</h2>
        <button type="button" onClick={() => onSelect(null)} className="text-[12px] text-muted-foreground hover:text-foreground">Clear</button>
      </div>
      <p className="mb-2 text-[12px] text-muted-foreground">
        {NODE_STYLE[node.type].label.replace(/s$/, '')}
        {node.workspace && <> · {node.workspace}</>}
        {node.status && <> · {node.status}</>}
        {node.at && <> · {relativeAge(node.at)}</>}
      </p>
      {href && <Link to={href} className="mb-3 inline-block text-[12px] text-primary hover:underline">{OPEN[node.type]} →</Link>}
      <div className="mb-2 flex items-center gap-1.5 text-[12px] text-muted-foreground">
        Focus
        {[1, 2].map((d) => (
          <button key={d} type="button" onClick={() => setDepth(d)} aria-pressed={depth === d}
                  className={`min-h-7 rounded-md px-2 ${depth === d ? 'bg-muted font-medium text-foreground' : ''}`}>
            {d === 1 ? 'neighbours' : 'two steps'}
          </button>
        ))}
      </div>
      <table className="w-full table-fixed text-[12px]">
        <thead><Tr rule="bottom" head="plain"><Th weight="normal">Connected to ({rows.length})</Th></Tr></thead>
        <tbody>
          {rows.map(({ e, other }) => (
            <Tr key={`${e.source}>${e.target}>${e.kind}>${e.proposed}`} rule="bottom-last">
              <Td>
                <button type="button" onClick={() => onSelect(other.id)} className="flex w-full min-w-0 items-center gap-1.5 text-left text-foreground hover:underline" title={other.label}>
                  <span className="inline-block size-2 shrink-0 rounded-full" style={{ background: `var(${NODE_STYLE[other.type].token})` }} />
                  <span className="truncate">{other.label}</span>
                </button>
                <div className="truncate pl-3.5 text-[11px] text-muted-foreground" title={(e.evidence ?? []).join('\n') || undefined}>
                  {describeEdge(e)}{e.weight > 1 && <> · {e.weight}</>}
                </div>
              </Td>
            </Tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="py-1 text-[12px] text-muted-foreground">Nothing it connects to is shown with these filters.</p>}
    </aside>
  )
}

/** No WebGL (an old browser, a locked-down machine, hardware acceleration off): the
 *  same graph as a table of connections. */
function NoWebGL({ view, byId, onSelect }: {
  view: { edges: GraphEdgeOut[] }; byId: Map<string, GraphNodeOut>; onSelect: (id: string) => void
}): JSX.Element {
  const rows = view.edges.slice(0, 500)
  const name = (id: string) => byId.get(id)?.label ?? id
  return (
    <div className="rounded-lg border border-border bg-card p-3 text-[12px]" style={{ maxHeight: HEIGHT, overflowY: 'auto' }}>
      <p className="mb-2 text-muted-foreground">
        This browser cannot draw 3-D (WebGL is off), so here are the same connections as a table.
        {view.edges.length > rows.length && ` The first ${rows.length} of ${view.edges.length}.`}
      </p>
      <table className="w-full">
        <thead><Tr rule="bottom" head="plain">
          <Th weight="normal">From</Th><Th weight="normal">How</Th><Th weight="normal">To</Th>
        </Tr></thead>
        <tbody>
          {rows.map((e) => (
            <Tr key={`${e.source}>${e.target}>${e.kind}>${e.proposed}`} rule="bottom-last">
              <Td density="compact-gutter"><button type="button" className="text-left text-foreground hover:underline" onClick={() => onSelect(e.source)}>{name(e.source)}</button></Td>
              <Td density="compact-gutter" tone="muted">{describeEdge(e)}</Td>
              <Td><button type="button" className="text-left text-foreground hover:underline" onClick={() => onSelect(e.target)}>{name(e.target)}</button></Td>
            </Tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
