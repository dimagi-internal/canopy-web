import { describe, expect, it } from 'vitest'

import type { AgentTopologyOut, RunnerTopologyOut } from '@/api/workspaces'
import { buildFleetMap, curve, hiddenBy, initials, initiallyCollapsed, shortRunner } from './topologyMap'

const runner = (id: string, workspace: string, paired: string, status = 'online', in_tree = true) => ({
  id, name: `${id}-mbp-cdp`, kind: 'emdash', location: 'local', status, ready: true, ready_note: '',
  paused: false, host: '', last_heartbeat_at: null, workspace, in_tree, owner_email: paired,
  flags: [], agent_count: 1,
})
const route = (runner_id: string, rank = 0) => ({
  runner_id, rank, enabled: true, source: '', actor: '', strict: false, turn_mode: '', can_claim: true,
})

const runnerTopo = {
  root: 'dimagi',
  workspaces: [
    { slug: 'dimagi', display_name: 'Dimagi', parent: null, depth: 0, agents: [] },
    { slug: 'connect', display_name: 'Connect', parent: 'dimagi', depth: 1, agents: [
      { slug: 'ada', name: 'Ada', turn_mode: 'manual', routes: [route('jj')] },
      { slug: 'jarvis', name: 'Jarvis', turn_mode: 'manual', routes: [] },
    ] },
    { slug: 'ops', display_name: 'Operations', parent: 'dimagi', depth: 1, agents: [] },
  ],
  runners: [runner('jj', 'dimagi', 'jj@x'), runner('st', 'connect', 'st@x'), runner('far', 'other', 'o@x', 'online', false)],
} as unknown as RunnerTopologyOut

const agentTopo = {
  root: 'dimagi',
  workspaces: [],
  agents: [
    { slug: 'ada', name: 'Ada', workspace: 'connect', owner_email: 'jj@x', login_email: 'ada@x', login_user_id: 2, interface_published: false, full_people: [] },
    { slug: 'jarvis', name: 'Jarvis', workspace: 'connect', owner_email: null, login_email: null, login_user_id: null, interface_published: false, full_people: [] },
  ],
  edges: [],
} as unknown as AgentTopologyOut

describe('buildFleetMap', () => {
  const map = buildFleetMap(runnerTopo, agentTopo)

  it('nests workspaces and groups each one by owner, unowned last', () => {
    expect(map.root?.slug).toBe('dimagi')
    expect(map.root?.children.map((c) => c.slug)).toEqual(['connect', 'ops'])
    const connect = map.root!.children[0]
    expect(connect.lanes.map((l) => l.owner)).toEqual(['jj@x', 'st@x', null])
    expect(connect.lanes[0].agents.map((a) => a.slug)).toEqual(['ada'])
    expect(connect.lanes[1].runners.map((r) => r.id)).toEqual(['st'])
  })

  it('rolls counts and problems up the tree', () => {
    expect(map.root?.agentCount).toBe(2)
    expect(map.root?.runnerCount).toBe(2)
    expect(map.root?.problemCount).toBe(1) // jarvis is unrouted
    expect(map.agents.get('ada')?.health).toBe('ok')
  })

  it('keeps runners from outside the tree apart', () => {
    expect(map.outside.map((r) => r.id)).toEqual(['far'])
  })

  it('starts empty workspaces collapsed, never the root', () => {
    expect([...initiallyCollapsed(map.root!)]).toEqual(['ops'])
  })

  it('finds the collapsed box that hides a workspace', () => {
    expect(hiddenBy(map.root!, 'connect', new Set())).toBeNull()
    expect(hiddenBy(map.root!, 'connect', new Set(['connect']))).toBe('connect')
    expect(hiddenBy(map.root!, 'connect', new Set(['dimagi', 'connect']))).toBe('dimagi')
  })
})

describe('labels', () => {
  it('shortens runner names and makes initials', () => {
    expect(shortRunner('acedimagi-mbp-cdp')).toBe('acedimagi')
    expect(shortRunner('cloud-ec2-1')).toBe('cloud-ec2-1')
    expect(initials('jjackson@dimagi.com')).toBe('JJ')
    expect(initials('sarvesh.tewari@x')).toBe('ST')
    expect(initials(null)).toBe('—')
  })
})

describe('curve', () => {
  it('runs from edge to edge, not centre to centre', () => {
    const g = curve({ x: 0, y: 0, w: 100, h: 50 }, { x: 300, y: 0, w: 100, h: 50 })!
    expect(g.d.startsWith('M 100 25 ')).toBe(true)
    expect(g.d.endsWith(' 300 25')).toBe(true)
  })
  it('draws nothing between a box and itself', () => {
    expect(curve({ x: 0, y: 0, w: 10, h: 10 }, { x: 0, y: 0, w: 10, h: 10 })).toBeNull()
  })
})
