import { describe, expect, it, vi } from 'vitest'

import {
  CanopyRestError,
  buildSessionWsUrl,
  createCanopyClient,
  createHostBridge,
  createTokenStore,
  UnknownActionError,
} from './index'

const TOKEN_TTL = () => new Date(Date.now() + 60 * 60 * 1000).toISOString()

describe('token store', () => {
  it('caches, so N callers in a tick do not mint N tokens server-side', async () => {
    const fetchToken = vi.fn().mockResolvedValue({ token: 't1', expiresAt: TOKEN_TTL() })
    const store = createTokenStore(fetchToken)

    const all = await Promise.all([store.get(), store.get(), store.get()])

    expect(all).toEqual(['t1', 't1', 't1'])
    expect(fetchToken).toHaveBeenCalledTimes(1)
  })

  it('force bypasses the cache — the 401 path must not trust our own bookkeeping', async () => {
    const fetchToken = vi
      .fn()
      .mockResolvedValueOnce({ token: 'stale', expiresAt: TOKEN_TTL() })
      .mockResolvedValueOnce({ token: 'fresh', expiresAt: TOKEN_TTL() })
    const store = createTokenStore(fetchToken)

    expect(await store.get()).toBe('stale')
    expect(await store.get(true)).toBe('fresh')
    expect(fetchToken).toHaveBeenCalledTimes(2)
  })

  it('refetches a token already inside the refresh skew', async () => {
    // 2 minutes out, against a 5-minute skew: treated as due, not valid.
    const soon = new Date(Date.now() + 2 * 60 * 1000).toISOString()
    const fetchToken = vi.fn().mockResolvedValue({ token: 't', expiresAt: soon })
    const store = createTokenStore(fetchToken)

    await store.get()
    await store.get()

    expect(fetchToken).toHaveBeenCalledTimes(2)
  })

  it('treats an unparseable expiry as already expired rather than caching NaN', async () => {
    const fetchToken = vi.fn().mockResolvedValue({ token: 't', expiresAt: 'not a date' })
    const store = createTokenStore(fetchToken)

    await store.get()
    await store.get()

    expect(fetchToken).toHaveBeenCalledTimes(2)
  })

  it('does not poison later calls with a rejected in-flight promise', async () => {
    const fetchToken = vi
      .fn()
      .mockRejectedValueOnce(new Error('network blip'))
      .mockResolvedValueOnce({ token: 'recovered', expiresAt: TOKEN_TTL() })
    const store = createTokenStore(fetchToken)

    await expect(store.get()).rejects.toThrow('network blip')
    expect(await store.get()).toBe('recovered')
  })

  it('keeps two stores independent — a page may mount two widgets', async () => {
    // ace-web held this state at module scope, which is fine for exactly one
    // client per page and wrong for two panels or two agents.
    const a = createTokenStore(vi.fn().mockResolvedValue({ token: 'a', expiresAt: TOKEN_TTL() }))
    const b = createTokenStore(vi.fn().mockResolvedValue({ token: 'b', expiresAt: TOKEN_TTL() }))

    expect(await a.get()).toBe('a')
    expect(await b.get()).toBe('b')
    expect(a.peek()).toBe('a')
  })

  it('peek is null before the first mint', () => {
    const store = createTokenStore(vi.fn())
    expect(store.peek()).toBeNull()
  })
})

describe('session websocket url', () => {
  it('borrows scheme and host when the base is a bare path', () => {
    const url = buildSessionWsUrl('/canopy', 'sess-1', 'tok', {
      protocol: 'https:',
      host: 'labs.connect.dimagi.com',
    })
    expect(url).toBe('wss://labs.connect.dimagi.com/canopy/ws/canopy-sessions/sess-1/?token=tok')
  })

  it('keeps its own host when the base is absolute — the widget case', () => {
    const url = buildSessionWsUrl('https://canopy.example.com/canopy', 'sess-1', 'tok')
    expect(url).toBe('wss://canopy.example.com/canopy/ws/canopy-sessions/sess-1/?token=tok')
  })

  it('downgrades to ws:// for a plain-http base, so dev works', () => {
    const url = buildSessionWsUrl('http://localhost:8000', 'sess-1', 'tok')
    expect(url).toBe('ws://localhost:8000/ws/canopy-sessions/sess-1/?token=tok')
  })

  it('url-encodes the session id and the token', () => {
    const url = buildSessionWsUrl('http://h', 'a/b', 'to ken')
    expect(url).toContain('/ws/canopy-sessions/a%2Fb/')
    expect(url).toContain('token=to%20ken')
  })

  it('omits the query entirely when there is no token', () => {
    expect(buildSessionWsUrl('http://h', 's', null)).toBe('ws://h/ws/canopy-sessions/s/')
  })
})

describe('rest', () => {
  function harness(responses: Array<{ status: number; body?: unknown }>) {
    const calls: Array<{ url: string; init: RequestInit }> = []
    const queue = [...responses]
    const fetchImpl = vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url: String(url), init })
      const next = queue.shift() ?? { status: 200, body: [] }
      return {
        ok: next.status >= 200 && next.status < 300,
        status: next.status,
        json: async () => next.body,
      } as Response
    }) as unknown as typeof fetch

    const client = createCanopyClient({
      baseUrl: 'https://canopy.example.com',
      fetchToken: vi.fn().mockResolvedValue({ token: 'tok', expiresAt: TOKEN_TTL() }),
      originKey: 'connect-labs:opp-42',
      source: 'connect-labs',
      fetchImpl,
    })
    return { client, calls }
  }

  it('sends the bearer on every request', async () => {
    const { client, calls } = harness([{ status: 200, body: [] }])
    await client.rest.listAgents()
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe('Bearer tok')
  })

  it('scopes the session list by this product, not by everything the user has', async () => {
    const { client, calls } = harness([{ status: 200, body: [] }])
    await client.rest.listSessions({ state: 'active' })
    expect(calls[0].url).toContain('origin_key=connect-labs%3Aopp-42')
    expect(calls[0].url).toContain('source=connect-labs')
    expect(calls[0].url).toContain('state=active')
  })

  it('retries a 401 exactly once, with a forced re-mint', async () => {
    const { client, calls } = harness([
      { status: 401 },
      { status: 200, body: [{ id: 's1', title: 't', last_activity_at: 'x' }] },
    ])
    const rows = await client.rest.listSessions()
    expect(calls).toHaveLength(2)
    expect(rows[0].id).toBe('s1')
  })

  it('does not retry a second 401 — it raises instead of looping', async () => {
    const { client, calls } = harness([{ status: 401 }, { status: 401 }])
    await expect(client.rest.listSessions()).rejects.toBeInstanceOf(CanopyRestError)
    expect(calls).toHaveLength(2)
  })

  it('maps last_activity_at onto updatedAt — SessionOut has no updated_at', async () => {
    const { client } = harness([
      {
        status: 200,
        body: [
          {
            id: 's1',
            title: 'chat',
            agent_slug: 'labs-helper',
            last_activity_at: '2026-09-12T00:00:00Z',
            runner_name: 'jj-mbp',
            runner_online: true,
          },
        ],
      },
    ])
    const [row] = await client.rest.listSessions()
    expect(row.updatedAt).toBe('2026-09-12T00:00:00Z')
    expect(row.agentSlug).toBe('labs-helper')
    expect(row.runnerOnline).toBe(true)
  })

  it('reports runnerOnline as null for an unbound session, not false', async () => {
    // null means "nothing to be offline"; false would make the placement banner
    // claim a runner had gone away when there never was one.
    const { client } = harness([{ status: 200, body: [{ id: 's', title: '', last_activity_at: '' }] }])
    const [row] = await client.rest.listSessions()
    expect(row.runnerOnline).toBeNull()
  })

  it('threads has_more_before through the detail read', async () => {
    const { client } = harness([
      {
        status: 200,
        body: { id: 's', title: '', last_activity_at: '', has_more_before: true, oldest_loaded_turn_index: 64 },
      },
    ])
    const detail = await client.rest.getSession('s')
    expect(detail.hasMoreBefore).toBe(true)
    expect(detail.oldestLoadedTurnIndex).toBe(64)
  })

  it('the agent picker takes no app parameter — the token decides', async () => {
    const { client, calls } = harness([{ status: 200, body: [] }])
    await client.rest.listAgents()
    expect(calls[0].url).toBe('https://canopy.example.com/api/embed/agents')
    expect(calls[0].url).not.toContain('app=')
  })
})

describe('socket url from the client', () => {
  it('is null until a token exists, rather than a url that will be rejected', async () => {
    const client = createCanopyClient({
      baseUrl: 'https://canopy.example.com',
      fetchToken: vi.fn().mockResolvedValue({ token: 'tok', expiresAt: TOKEN_TTL() }),
      fetchImpl: vi.fn() as unknown as typeof fetch,
    })
    expect(client.sessionSocketUrl('s1')).toBeNull()

    await client.rest.listAgents().catch(() => undefined) // mints a token
    expect(client.sessionSocketUrl('s1')).toContain('token=tok')
  })
})

describe('host bridge', () => {
  it('returns an empty snapshot when the host lends no context', async () => {
    expect(await createHostBridge().readContext()).toEqual({})
  })

  it('reads the CURRENT page state each time, not the state at registration', async () => {
    // The whole point: a workflow's props change as the user works, and the
    // agent must see what is on screen when the session opens.
    const bridge = createHostBridge()
    let step = 1
    bridge.provideContext(() => ({ step }))
    expect(await bridge.readContext()).toEqual({ step: 1 })
    step = 2
    expect(await bridge.readContext()).toEqual({ step: 2 })
  })

  it('replaces the provider rather than accumulating providers', async () => {
    const bridge = createHostBridge()
    bridge.provideContext(() => ({ which: 'first' }))
    bridge.provideContext(() => ({ which: 'second' }))
    expect(await bridge.readContext()).toEqual({ which: 'second' })
  })

  it('awaits an async provider', async () => {
    const bridge = createHostBridge()
    bridge.provideContext(async () => ({ loaded: true }))
    expect(await bridge.readContext()).toEqual({ loaded: true })
  })

  it('runs a registered action and returns its result to the agent', async () => {
    const bridge = createHostBridge()
    const onUpdateState = vi.fn().mockResolvedValue({ saved: true })
    bridge.registerAction('updateState', onUpdateState)

    const result = await bridge.runAction('updateState', { status: 'reviewed' })

    expect(onUpdateState).toHaveBeenCalledWith({ status: 'reviewed' })
    expect(result).toEqual({ saved: true })
  })

  it('rejects an unknown action instead of silently doing nothing', async () => {
    // A no-op is indistinguishable to the agent from success, and it will carry
    // on as though the page changed.
    await expect(createHostBridge().runAction('nope')).rejects.toBeInstanceOf(UnknownActionError)
  })

  it('rejects an action that was withdrawn when the page moved on', async () => {
    const bridge = createHostBridge()
    bridge.registerAction('updateState', vi.fn())
    bridge.unregisterAction('updateState')
    await expect(bridge.runAction('updateState')).rejects.toBeInstanceOf(UnknownActionError)
    expect(bridge.actionNames()).toEqual([])
  })

  it('re-registering a name replaces the handler, so a re-render cannot stack them', async () => {
    const bridge = createHostBridge()
    const stale = vi.fn()
    const fresh = vi.fn()
    bridge.registerAction('act', stale)
    bridge.registerAction('act', fresh)

    await bridge.runAction('act')

    expect(stale).not.toHaveBeenCalled()
    expect(fresh).toHaveBeenCalledOnce()
    expect(bridge.actionNames()).toEqual(['act'])
  })

  it('lists action names in a stable order across registration orders', () => {
    const a = createHostBridge()
    a.registerAction('zed', vi.fn())
    a.registerAction('alpha', vi.fn())
    expect(a.actionNames()).toEqual(['alpha', 'zed'])
  })

  it('propagates a host refusal as an error, not a falsy return', async () => {
    const bridge = createHostBridge()
    bridge.registerAction('act', () => {
      throw new Error('not allowed on a completed run')
    })
    await expect(bridge.runAction('act')).rejects.toThrow('not allowed on a completed run')
  })
})
