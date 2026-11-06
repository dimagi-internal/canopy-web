import { useCallback, useEffect, useLayoutEffect, useRef, useState, type JSX } from 'react'
import { edgeKey } from '@/pages/agentTopology'
import { curve, hiddenBy, type FleetMap } from '@/pages/topology/topologyMap'
import { type Selection } from '@/pages/topology/map/shared'
import { addKey } from '@/pages/topology/map/OwnerLane'
import { WorkspaceBox } from '@/pages/topology/map/WorkspaceBox'
import { RunnerChip } from '@/pages/topology/map/RunnerChip'

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

export function MapCanvas({
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
        targets.set(key, { el, tone, names: [a.name], label: e.access === 'full' ? 'all' : `${e.profiles.join(', ')} only` })
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
