import { useEffect, useRef, useState, type JSX } from 'react'
import ForceGraph3D, { type ForceGraph3DInstance } from '3d-force-graph'
import * as THREE from 'three'

import type { GraphEdgeOut, GraphNodeOut } from '@/api/workspaces'
import {
  edgeOpacity, edgeToken, edgeWidth, EDGE_STYLE, NODE_STYLE, nodeSize, type Shape, type View,
} from './relationGraph'

// The 3-D canvas of the relations view, on three.js through 3d-force-graph. Loaded
// lazily (RelationsView), so three.js is in its own chunk and no other page pays for
// it. Orbit controls: drag turns, scroll or pinch zooms, right-drag pans — what a
// laptop trackpad does without a mouse.

type Node3D = GraphNodeOut & { x?: number; y?: number; z?: number }
type Link3D = Omit<GraphEdgeOut, 'source' | 'target'> & { source: string | Node3D; target: string | Node3D }

const endId = (end: string | Node3D) => (typeof end === 'string' ? end : end.id)

/** A token's colour as rgb, whatever the theme writes it in (oklch included): drawn
 *  once on a 1×1 canvas and read back, since three.js parses only sRGB forms. */
function resolver(): (token: string) => [number, number, number] {
  const css = getComputedStyle(document.documentElement)
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 1
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  const memo = new Map<string, [number, number, number]>()
  return (token) => {
    if (memo.has(token)) return memo.get(token)!
    let rgb: [number, number, number] = [128, 128, 128]
    const value = css.getPropertyValue(token).trim()
    if (ctx && value) {
      ctx.clearRect(0, 0, 1, 1)
      ctx.fillStyle = value
      ctx.fillRect(0, 0, 1, 1)
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
      rgb = [r, g, b]
    }
    memo.set(token, rgb)
    return rgb
  }
}

function geometry(shape: Shape, r: number): THREE.BufferGeometry {
  switch (shape) {
    case 'octahedron': return new THREE.OctahedronGeometry(r)
    case 'box': return new THREE.BoxGeometry(r * 1.4, r * 1.4, r * 1.4)
    case 'dodecahedron': return new THREE.DodecahedronGeometry(r)
    case 'tetrahedron': return new THREE.TetrahedronGeometry(r * 1.2)
    case 'torus': return new THREE.TorusGeometry(r, r * 0.25, 8, 24)
    case 'cone': return new THREE.ConeGeometry(r * 0.8, r * 1.6, 12)
    default: return new THREE.SphereGeometry(r, 16, 12)
  }
}

const escape = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`)

/** Bumps whenever the theme class on <html> changes, so colours are read again. */
function useThemeKey(): number {
  const [key, setKey] = useState(0)
  useEffect(() => {
    const mo = new MutationObserver(() => setKey((k) => k + 1))
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme', 'style'] })
    return () => mo.disconnect()
  }, [])
  return key
}

export interface CanvasProps {
  view: View
  selected: string | null
  /** The selected node's neighbourhood; null lights everything. */
  lit: Set<string> | null
  onSelect: (id: string | null) => void
  height: number
}

export default function RelationsCanvas3D({ view, selected, lit, onSelect, height }: CanvasProps): JSX.Element {
  const wrapRef = useRef<HTMLDivElement>(null)
  const fgRef = useRef<ForceGraph3DInstance | null>(null)
  const meshes = useRef(new Map<string, THREE.Mesh>())
  // The same object per node id across filter changes, so a node keeps its place.
  const nodeCache = useRef(new Map<string, Node3D>())
  const fitted = useRef(false)
  const onSelectRef = useRef(onSelect)
  // How a node's material is painted now; a mesh made later (the library builds them
  // after a data change, not at once) is painted by the same rule.
  const paintRef = useRef<(n: GraphNodeOut, m: THREE.MeshLambertMaterial) => void>(() => {})
  const themeKey = useThemeKey()
  useEffect(() => { onSelectRef.current = onSelect }, [onSelect])

  // The graph itself: made once, torn down with the component.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const fg = new ForceGraph3D(el, { controlType: 'orbit' })
      .showNavInfo(false)
      .enableNodeDrag(false)
      .width(el.clientWidth)
      .height(height)
      .nodeLabel((n) => {
        const node = n as Node3D
        return `<div style="font:12px sans-serif"><b>${escape(node.label)}</b><br/>${escape(NODE_STYLE[node.type].label)}${node.workspace ? ` · ${escape(node.workspace)}` : ''}</div>`
      })
      .linkLabel((l) => escape(EDGE_STYLE[(l as Link3D).kind].label))
      .onNodeClick((n) => onSelectRef.current((n as Node3D).id))
      .onBackgroundClick(() => onSelectRef.current(null))
      .cooldownTicks(200)
      .onEngineStop(() => {
        if (!fitted.current) { fitted.current = true; fg.zoomToFit(600, 40) }
      })
    fgRef.current = fg
    const meshMap = meshes.current
    const ro = new ResizeObserver(() => fg.width(el.clientWidth))
    ro.observe(el)
    return () => {
      ro.disconnect()
      fg._destructor()
      fgRef.current = null
      meshMap.clear()
      el.replaceChildren()
    }
  }, [height])

  // What to draw.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    const cache = nodeCache.current
    const nodes = view.nodes.map((n) => {
      const kept = cache.get(n.id)
      const node: Node3D = kept ? Object.assign(kept, n) : { ...n }
      cache.set(n.id, node)
      return node
    })
    meshes.current.clear()
    fg.nodeThreeObject((n) => {
      const node = n as Node3D
      const style = NODE_STYLE[node.type]
      const material = new THREE.MeshLambertMaterial({ transparent: true, opacity: 0.95 })
      paintRef.current(node, material)
      const mesh = new THREE.Mesh(geometry(style.shape, nodeSize(node)), material)
      meshes.current.set(node.id, mesh)
      return mesh
    })
    fitted.current = false
    fg.graphData({ nodes, links: view.edges.map((e) => ({ ...e })) as unknown as Link3D[] })
  }, [view])

  // Colour, highlight and the selected node's neighbourhood: no new objects, only
  // their materials and the link accessors.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    const rgb = resolver()
    const rgba = (token: string, a: number) => `rgba(${rgb(token).join(',')},${a})`
    const [br, bg, bb] = rgb('--card')
    fg.backgroundColor(`rgb(${br},${bg},${bb})`)
    paintRef.current = (n, material) => {
      const [r, g, b] = rgb(NODE_STYLE[n.type].token)
      const glow = n.id === selected ? 0.25 : 0
      material.color.setRGB(r / 255, g / 255, b / 255, THREE.SRGBColorSpace)
      material.opacity = !lit || lit.has(n.id) ? (n.id === selected ? 1 : 0.92) : 0.12
      material.emissive.setRGB(glow, glow, glow)
    }
    for (const n of view.nodes) {
      const mesh = meshes.current.get(n.id)
      if (mesh) paintRef.current(n, mesh.material as THREE.MeshLambertMaterial)
    }
    const on = (l: Link3D) => !lit || (lit.has(endId(l.source)) && lit.has(endId(l.target)))
    const edge = (l: object) => l as Link3D as unknown as GraphEdgeOut
    fg.linkColor((l) => rgba(edgeToken(edge(l)), on(l as Link3D) ? edgeOpacity(edge(l)) : 0.04))
      .linkWidth((l) => edgeWidth(edge(l)) * (lit && on(l as Link3D) ? 1.5 : 1))
      .linkCurvature((l) => ((l as Link3D).proposed ? 0.25 : 0))
      .linkDirectionalParticles((l) => ((l as Link3D).proposed ? 2 : 0))
      .linkDirectionalParticleWidth(1.2)
      .linkDirectionalParticleColor((l) => rgba(edgeToken(edge(l)), 0.9))
  }, [view, lit, selected, themeKey])

  // Fly to what was picked (a click, or a search).
  useEffect(() => {
    const fg = fgRef.current
    const node = selected ? nodeCache.current.get(selected) : undefined
    if (!fg || !node || node.x === undefined) return
    const { x = 0, y = 0, z = 0 } = node
    const ratio = 1 + 220 / Math.max(1, Math.hypot(x, y, z))
    fg.cameraPosition({ x: x * ratio, y: y * ratio, z: z * ratio }, { x, y, z }, 900)
  }, [selected])

  return (
    <div className="relative overflow-hidden rounded-lg border border-border bg-card" style={{ height }}>
      <div ref={wrapRef} role="img" aria-label="3-D graph of people, agents, sessions, projects and tasks and how they relate" />
      <div className="absolute right-2 top-2 flex gap-1">
        <button type="button" onClick={() => fgRef.current?.zoomToFit(600, 40)}
                className="min-h-8 rounded-md border border-border bg-card px-2 text-[12px] text-foreground-secondary hover:text-foreground">
          Fit
        </button>
      </div>
      <p className="pointer-events-none absolute bottom-2 left-3 text-[11px] text-muted-foreground">
        Drag to turn · scroll or pinch to zoom · right-drag (two-finger click) to pan · click a shape to focus it
      </p>
    </div>
  )
}
