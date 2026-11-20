import { describe, expect, it } from 'vitest'

import type { GraphEdgeOut, GraphNodeOut } from '@/api/workspaces'
import {
  adjacency, applyFilters, defaultFilters, describeEdge, edgeToken, edgeWidth, hrefOf, neighbourhood, present,
  searchNodes, type Filters,
} from './relationGraph'

const n = (id: string, over: Partial<GraphNodeOut> = {}): GraphNodeOut => {
  const [type, ref] = id.split(':') as [GraphNodeOut['type'], string]
  return { id, type, label: ref, workspace: type === 'person' ? '' : 'acme', ref, at: null, weight: 1,
    sensitive: false, status: '', ...over }
}
const e = (source: string, target: string, kind: GraphEdgeOut['kind'], over: Partial<GraphEdgeOut> = {}): GraphEdgeOut => ({
  source, target, kind, weight: 1, at: null, tier: '', how: '', proposed: false, evidence: [], confidence: null, ...over,
})

const data = {
  nodes: [
    n('person:1', { label: 'Alice' }), n('person:2', { label: 'Bob' }), n('person:3', { label: 'Alone' }),
    n('agent:eva', { label: 'Eva', weight: 9 }), n('session:s1', { label: 'deploy supply' }),
    n('session:s2', { label: 'other', workspace: 'child' }), n('project:7', { label: 'Supply', ref: 'supply' }),
    n('task:ACM-1', { label: 'ACM-1 Ship it', ref: 'ACM-1' }), n('workspace:acme', { label: 'Acme', ref: 'acme' }),
    n('workspace:child', { label: 'Child', ref: 'child', workspace: 'child' }),
  ],
  edges: [
    e('person:1', 'session:s1', 'started'), e('person:2', 'session:s1', 'participates'),
    e('agent:eva', 'session:s1', 'ran'), e('task:ACM-1', 'session:s1', 'dispatched_from'),
    e('session:s1', 'project:7', 'linked', { tier: 'core', how: 'dispatched' }),
    e('session:s2', 'project:7', 'linked', { tier: 'candidate', how: 'signals' }),
    e('session:s2', 'project:7', 'linked', { tier: 'adjacent', how: 'signals', proposed: true }),
    e('person:2', 'person:1', 'reports_to'), e('person:1', 'session:s2', 'participates'),
    e('project:7', 'workspace:acme', 'within'),
  ],
}

const filters = (over: Partial<Filters> = {}): Filters => ({ ...defaultFilters(), ...over })
const ids = (xs: { id: string }[]) => xs.map((x) => x.id).sort()

describe('relationGraph filters', () => {
  it('by default keeps every type, every kind but structure, the core/adjacent/candidate tiers, no proposals', () => {
    const v = applyFilters(data, filters())
    expect(v.edges.some((x) => x.kind === 'within')).toBe(false)
    expect(v.edges.some((x) => x.proposed)).toBe(false)
    expect(v.edges.filter((x) => x.kind === 'linked').map((x) => x.tier).sort()).toEqual(['candidate', 'core'])
    // Nothing touches the lone person, so they are left out; workspaces only through `within`.
    expect(ids(v.nodes)).not.toContain('person:3')
    expect(ids(v.nodes)).not.toContain('workspace:acme')
  })

  it('drops a node type with every edge to it', () => {
    const v = applyFilters(data, filters({ types: new Set(['person', 'session', 'project']) }))
    expect(ids(v.nodes)).toEqual(['person:1', 'person:2', 'project:7', 'session:s1', 'session:s2'])
    expect(v.edges.every((x) => !x.source.startsWith('agent:') && !x.source.startsWith('task:'))).toBe(true)
  })

  it('narrows project links by tier, and shows proposals only when asked', () => {
    const coreOnly = applyFilters(data, filters({ tiers: new Set(['core']) }))
    expect(coreOnly.edges.filter((x) => x.kind === 'linked')).toHaveLength(1)
    const withProposed = applyFilters(data, filters({ proposed: true }))
    expect(withProposed.edges.filter((x) => x.proposed)).toHaveLength(1)
    const proposedButTierOff = applyFilters(data, filters({ proposed: true, tiers: new Set(['core']) }))
    expect(proposedButTierOff.edges.some((x) => x.proposed)).toBe(false)
  })

  it('narrows by edge kind, and keeps isolated nodes when told to', () => {
    const v = applyFilters(data, filters({ kinds: new Set(['reports_to']), hideIsolated: false }))
    expect(v.edges.map((x) => x.kind)).toEqual(['reports_to'])
    expect(ids(v.nodes)).toContain('person:3')
    expect(ids(applyFilters(data, filters({ kinds: new Set(['reports_to']) })).nodes)).toEqual(['person:1', 'person:2'])
  })

  it('narrows to one workspace, keeping a person only when they touch something in it', () => {
    const v = applyFilters(data, filters({ workspace: 'child', hideIsolated: false }))
    expect(ids(v.nodes)).toEqual(['person:1', 'session:s2', 'workspace:child'])
    expect(v.edges.map((x) => `${x.source}>${x.target}`)).toEqual(['person:1>session:s2'])
  })

  it('offers only what is present', () => {
    const p = present(data)
    expect(p.workspaces).toEqual(['acme', 'child'])
    expect(p.tiers.has('rejected')).toBe(false)
    expect(p.kinds.has('coaches')).toBe(false)
  })
})

describe('relationGraph neighbourhood, search and links', () => {
  it('finds a node\'s neighbourhood one or two steps out', () => {
    const adj = adjacency(applyFilters(data, filters()))
    expect([...neighbourhood(adj, 'person:2')].sort()).toEqual(['person:1', 'person:2', 'session:s1'])
    expect(neighbourhood(adj, 'person:2', 2).has('agent:eva')).toBe(true)
    expect([...neighbourhood(adj, 'nobody')]).toEqual(['nobody'])
  })

  it('searches labels, prefix first, then the most connected', () => {
    expect(searchNodes(data.nodes, 'su').map((x) => x.id)).toEqual(['project:7', 'session:s1'])
    expect(searchNodes(data.nodes, 'acm-1').map((x) => x.id)).toEqual(['task:ACM-1'])
    expect(searchNodes(data.nodes, '  ')).toEqual([])
    expect(searchNodes(data.nodes, 'e', 2)).toHaveLength(2)
  })

  it('links each node to its own page', () => {
    expect(hrefOf(n('person:1'), 'acme')).toBe('/w/acme/members/1')
    expect(hrefOf(n('session:s2', { workspace: 'child' }), 'acme')).toBe('/w/child/sessions/s2')
    expect(hrefOf(n('project:7', { ref: 'supply' }), 'acme')).toBe('/w/acme/projects/supply')
    expect(hrefOf(n('task:ACM-1'), 'acme')).toBe('/w/acme/tasks/ACM-1')
    expect(hrefOf(n('agent:eva'), 'acme')).toBe('/w/acme/agents/eva')
    expect(hrefOf(n('output:3'), 'acme')).toBeNull()
  })

  it('styles a project link by its tier and says what a proposal is', () => {
    const core = e('a', 'b', 'linked', { tier: 'core', how: 'dispatched' })
    const cand = e('a', 'b', 'linked', { tier: 'candidate' })
    expect(edgeWidth(core)).toBeGreaterThan(edgeWidth(cand))
    expect(edgeToken(e('a', 'b', 'linked', { tier: 'rejected' }))).toBe('--destructive')
    expect(describeEdge(core)).toBe('Session tied to a project · core (dispatched)')
    expect(describeEdge({ ...cand, proposed: true })).toMatch(/^Would be: /)
  })
})
