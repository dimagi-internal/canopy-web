import { describe, expect, it } from 'vitest'

import type { NetworkNodeOut, WorkspaceNetworkOut } from '@/api/workspaces'
import { buildGraph, categoryTokens, hrefOf, linkTurns, profile, tokenOf, workspaceAnchors } from './networkGraph'

const node = (over: Partial<NetworkNodeOut>): NetworkNodeOut => ({
  id: 'x', kind: 'thread', label: 'x', workspace: 'dimagi', workspaces: ['dimagi'], turns: 1, threads: 1,
  channel: 'chat', ref: '', agent: '', openable: false, ...over,
})

describe('networkGraph', () => {
  it('links writers and agents through threads, dropping links to nodes it was not sent', () => {
    const data = {
      days: 7, since: '', workspaces: ['dimagi'], truncated: false,
      totals: { people: 1, contacts: 0, agents: 1, threads: 1, shared_pairs: 0, shared_threads: 0, threads_seen: 1 },
      nodes: [node({ id: 'person:1', kind: 'person' }), node({ id: 'session:a' }), node({ id: 'agent:hal', kind: 'agent' })],
      links: [
        { source: 'person:1', target: 'session:a', turns: 2, kind: 'wrote' },
        { source: 'session:a', target: 'agent:hal', turns: 2, kind: 'answered' },
        { source: 'person:9', target: 'session:a', turns: 1, kind: 'wrote' },
      ],
    } as WorkspaceNetworkOut
    const g = buildGraph(data)
    expect(g.links).toHaveLength(2)
    expect([...g.neighbours.get('session:a')!].sort()).toEqual(['agent:hal', 'person:1'])
    expect(linkTurns(g, 'session:a', 'person:1')).toBe(2)
    expect(linkTurns(g, 'person:1', 'session:a')).toBe(2)
  })

  it('opens a chat only when the viewer may, and an agent at its page', () => {
    expect(hrefOf(node({ id: 'session:a', ref: 'a', openable: true }))).toBe('/w/dimagi/sessions/a')
    expect(hrefOf(node({ id: 'session:a', ref: 'a', openable: false }))).toBeNull()
    expect(hrefOf(node({ id: 'mail:m', ref: 'm', agent: 'hal' }))).toBe('/w/dimagi/agents/hal/mail')
    expect(hrefOf(node({ id: 'agent:hal', kind: 'agent', ref: 'hal', workspace: 'connect' }))).toBe('/w/connect/agents/hal')
    expect(hrefOf(node({ id: 'person:1', kind: 'person' }))).toBeNull()
  })

  it('colours by kind, channel or workspace from semantic tokens', () => {
    const cats = categoryTokens(['slack', 'chat', 'slack'])
    expect([...cats.keys()]).toEqual(['slack', 'chat'])
    const slack = node({ channel: 'slack' })
    expect(tokenOf(slack, 'kind', cats)).toBe('--info')
    expect(tokenOf(slack, 'channel', cats)).toBe(cats.get('slack'))
    expect(tokenOf(node({ kind: 'person' }), 'channel', cats)).toBe('--success')
    const ws = categoryTokens(['dimagi', 'connect'])
    expect(tokenOf(node({ kind: 'person', workspace: 'connect' }), 'workspace', ws)).toBe(ws.get('connect'))
  })

  it('spreads workspaces apart only when there is more than one', () => {
    expect(workspaceAnchors(['dimagi'], 100).get('dimagi')).toEqual({ x: 0, y: 0 })
    const two = workspaceAnchors(['a', 'b'], 100)
    expect(two.get('a')!.x).toBeCloseTo(100)
    expect(two.get('b')!.x).toBeCloseTo(-100)
  })
  it('profiles who a node works with, through which agents and channels', () => {
    const data = {
      days: 7, since: '', workspaces: ['dimagi'], truncated: false,
      totals: { people: 2, contacts: 1, agents: 2, threads: 3, shared_pairs: 1, shared_threads: 1, threads_seen: 3 },
      nodes: [
        node({ id: 'person:1', kind: 'person' }), node({ id: 'person:2', kind: 'person' }),
        node({ id: 'contact:5', kind: 'contact' }),
        node({ id: 'agent:hal', kind: 'agent', label: 'Hal' }), node({ id: 'agent:eva', kind: 'agent' }),
        node({ id: 'session:a', turns: 5 }), node({ id: 'session:b', channel: 'slack', turns: 1 }),
        node({ id: 'session:c', channel: 'email', turns: 2 }),
      ],
      links: [
        { source: 'person:1', target: 'session:a', turns: 3, kind: 'wrote' },
        { source: 'person:2', target: 'session:a', turns: 1, kind: 'wrote' },
        { source: 'agent:eva', target: 'session:a', turns: 1, kind: 'wrote' },
        { source: 'session:a', target: 'agent:hal', turns: 5, kind: 'answered' },
        { source: 'person:1', target: 'session:b', turns: 1, kind: 'wrote' },
        { source: 'session:b', target: 'agent:hal', turns: 1, kind: 'answered' },
        { source: 'contact:5', target: 'session:c', turns: 2, kind: 'wrote' },
        { source: 'session:c', target: 'agent:eva', turns: 2, kind: 'answered' },
      ],
    } as WorkspaceNetworkOut
    const g = buildGraph(data)

    const me = profile(g, 'person:1')
    expect(me.people.map((t) => [t.node.id, t.threads])).toEqual([['person:2', 1]])
    expect(me.agents.map((t) => [t.node.id, t.threads, t.turns])).toEqual([['agent:hal', 2, 4], ['agent:eva', 1, 1]])
    expect(me.channels).toEqual([{ channel: 'chat', threads: 1 }, { channel: 'slack', threads: 1 }])
    expect(me.threads.map((r) => [r.node.id, r.turns, r.agent?.id, r.others])).toEqual([
      ['session:a', 3, 'agent:hal', 1], ['session:b', 1, 'agent:hal', 0],
    ])

    // An agent is used by the people in the threads it answered, and wrote in Hal's.
    const eva = profile(g, 'agent:eva')
    expect(eva.people.map((t) => t.node.id)).toEqual(['contact:5'])
    expect(eva.agents.map((t) => t.node.id)).toEqual(['agent:hal'])
    expect(eva.threads.map((r) => r.node.id).sort()).toEqual(['session:a', 'session:c'])

    expect(profile(g, 'session:a').writers.map((t) => [t.node.id, t.turns]))
      .toEqual([['person:1', 3], ['person:2', 1], ['agent:eva', 1]])
  })
})
