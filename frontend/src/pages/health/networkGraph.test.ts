import { describe, expect, it } from 'vitest'

import type { NetworkNodeOut, WorkspaceNetworkOut } from '@/api/workspaces'
import { buildGraph, categoryTokens, hrefOf, linkTurns, tokenOf, workspaceAnchors } from './networkGraph'

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
    expect(hrefOf(node({ id: 'session:a', ref: 'a', openable: true }))).toBe('/w/dimagi/chat/a')
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
})
