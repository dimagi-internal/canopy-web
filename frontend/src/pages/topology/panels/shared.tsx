import { type JSX, type ReactNode } from 'react'
import { type MapWorkspace } from '@/pages/topology/topologyMap'

export function PanelHeading({ children }: { children: ReactNode }): JSX.Element {
  return <h4 className="m-0 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{children}</h4>
}

export function findWorkspace(ws: MapWorkspace | null, slug: string): MapWorkspace | null {
  if (!ws) return null
  if (ws.slug === slug) return ws
  for (const c of ws.children) {
    const hit = findWorkspace(c, slug)
    if (hit) return hit
  }
  return null
}

