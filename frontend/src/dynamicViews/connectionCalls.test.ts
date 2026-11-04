import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/problem'
import { CallQueue, ConnectionCalls, MAX_IN_FLIGHT, NEEDS_CLICK, pageClickedFrame, QUEUE_WAIT_MS, type CallContext, type ConnectionClient } from './connectionCalls'

const DECLARED = [{
  provider: 'salesforce', as: 'viewer',
  reads: [{ method: 'GET', path: 'query' }, { method: 'POST', path: 'analytics/reports/*' }],
  writes: [{ method: 'PATCH', path: 'sobjects/Lead/*' }],
}]
const VIEW = { kind: 'view' as const, id: 7, version: 3 }
const READ = { provider: 'salesforce', method: 'GET', path: 'query', query: { q: 'SELECT Id FROM Lead' } }
const WRITE = { provider: 'salesforce', method: 'PATCH', path: 'sobjects/Lead/00Q1', body: { Status: 'Open' } }

function setup(opts: { active?: boolean | undefined; ctx?: Partial<CallContext>; now?: () => number } = {}) {
  const state = { active: opts.active, now: 1_000_000 }
  const client: ConnectionClient = {
    request: vi.fn(async () => ({ status: 200, body: { ok: true } })),
    openOperation: vi.fn(async () => ({ operation_id: `op${(client.openOperation as ReturnType<typeof vi.fn>).mock.calls.length}`, expires_at: new Date(state.now + 120_000).toISOString(), hard_limit_at: new Date(state.now + 600_000).toISOString(), writes: [{ provider: 'salesforce', method: 'PATCH', path: 'sobjects/Lead/*' }] })),
  }
  const ctx: CallContext = { surface: VIEW, declared: DECLARED, canConnect: true, ...opts.ctx }
  const queue = new CallQueue()
  const calls = new ConnectionCalls(client, () => ctx, { isActive: () => state.active, now: opts.now ?? (() => state.now), queue })
  return { calls, client, state, ctx, queue }
}

describe('ConnectionCalls', () => {
  it('read goes through', async () => {
    const { calls, client } = setup({ active: false })
    await expect(calls.handle(READ)).resolves.toEqual({ status: 200, body: { ok: true } })
    expect(client.openOperation).not.toHaveBeenCalled()
    expect(client.request).toHaveBeenCalledWith('salesforce', expect.objectContaining({
      method: 'GET', path: 'query', surface: VIEW, as: 'viewer',
    }), expect.anything())
  })

  it('a report run is a read: a declared POST read needs no click', async () => {
    const { calls, client } = setup({ active: false })
    await calls.handle({ provider: 'salesforce', method: 'POST', path: 'analytics/reports/00O1' })
    expect(client.openOperation).not.toHaveBeenCalled()
    expect(client.request).toHaveBeenCalled()
  })

  it('undeclared refused with no request', async () => {
    const { calls, client } = setup({ active: true })
    await expect(calls.handle({ provider: 'salesforce', method: 'DELETE', path: 'sobjects/Lead/00Q1' })).rejects.toThrow("This screen didn't declare that call.")
    await expect(calls.handle({ provider: 'salesforce', method: 'GET', path: 'search' })).rejects.toThrow("didn't declare")
    expect(client.request).not.toHaveBeenCalled()
    expect(client.openOperation).not.toHaveBeenCalled()
  })

  it('write with no page activation refused with no request', async () => {
    const { calls, client } = setup({ active: false })
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
    expect(client.openOperation).not.toHaveBeenCalled()
    expect(client.request).not.toHaveBeenCalled()
  })

  it('a message claiming a click changes nothing', async () => {
    const { calls, client } = setup({ active: false })
    await expect(calls.handle({ ...WRITE, click: true, userActivation: { isActive: true }, active: true, operation_id: 'forged' }))
      .rejects.toThrow('This needs a click.')
    expect(client.request).not.toHaveBeenCalled()
    // and a claim never reaches the server on an allowed call
    const ok = setup({ active: true })
    await ok.calls.handle({ ...WRITE, click: true, operation_id: 'forged' })
    const body = (ok.client.request as ReturnType<typeof vi.fn>).mock.calls[0][1]
    expect(body.operation_id).toBe('op1')
    expect(body).not.toHaveProperty('click')
  })

  it('absent userActivation fails closed', async () => {
    const { calls, client } = setup({ active: undefined })
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
    expect(client.request).not.toHaveBeenCalled()
  })

  it('write with activation asks for one operation and reuses it', async () => {
    const { calls, client, state } = setup({ active: true })
    await calls.handle(WRITE)
    state.active = false // the click is over; the lease stays
    await calls.handle({ ...WRITE, path: 'sobjects/Lead/00Q2' })
    expect(client.openOperation).toHaveBeenCalledTimes(1)
    expect(client.openOperation).toHaveBeenCalledWith(VIEW, expect.anything())
    const sent = (client.request as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1].operation_id)
    expect(sent).toEqual(['op1', 'op1'])
  })

  it('two writes in one click share one operation', async () => {
    const { calls, client } = setup({ active: true })
    await Promise.all([calls.handle(WRITE), calls.handle({ ...WRITE, path: 'sobjects/Lead/00Q2' })])
    expect(client.openOperation).toHaveBeenCalledTimes(1)
  })

  it('expired operation asks again only with activation', async () => {
    const { calls, client, state } = setup({ active: true })
    await calls.handle(WRITE)
    state.now += 121_000
    state.active = false
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
    expect(client.request).toHaveBeenCalledTimes(1)
    state.active = true
    await calls.handle(WRITE)
    expect(client.openOperation).toHaveBeenCalledTimes(2)
  })

  it('an operation is forgotten when the view changes', async () => {
    const { calls, client, ctx, state } = setup({ active: true })
    await calls.handle(WRITE)
    ctx.surface = { kind: 'view', id: 7, version: 4 }
    state.active = false
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
    expect(client.request).toHaveBeenCalledTimes(1)
  })

  it('public door refused', async () => {
    const { calls, client } = setup({ active: true, ctx: { canConnect: false, surface: { kind: 'app', workspace: 'w', key: 'k' } } })
    await expect(calls.handle(READ)).rejects.toThrow("A public page can't use connections.")
    expect(client.request).not.toHaveBeenCalled()
    expect(client.openOperation).not.toHaveBeenCalled()
  })

  it('a preview with no saved version asks for a save', async () => {
    const { calls, client } = setup({ active: true, ctx: { surface: null } })
    await expect(calls.handle(READ)).rejects.toThrow('Save this view to use connections.')
    expect(client.request).not.toHaveBeenCalled()
  })

  it('an app sends its workspace and key, and the server decides what it declared', async () => {
    const app = { kind: 'app' as const, workspace: 'dimagi', key: 'crm' }
    const { calls, client } = setup({ active: false, ctx: { surface: app, declared: undefined } })
    await calls.handle(READ)
    expect((client.request as ReturnType<typeof vi.fn>).mock.calls[0][1].surface).toEqual(app)
  })

  it('refusal sentence reaches the frame', async () => {
    const { calls, client } = setup({ active: false })
    const sentence = 'Connect Salesforce first, under Settings.'
    ;(client.request as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(409, sentence, { error: 'not_connected', detail: sentence }),
    )
    await expect(calls.handle(READ)).rejects.toThrow(sentence)
  })

  it('a 404 with no error member gets a plain sentence', async () => {
    const { calls, client } = setup({ active: false })
    ;(client.request as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(404, 'Not Found', { title: 'Not Found' }))
    await expect(calls.handle(READ)).rejects.toThrow("This screen can't reach that.")
  })

  it('a server that says a write needs an operation drops the lease', async () => {
    const { calls, client, state } = setup({ active: true })
    await calls.handle(WRITE)
    ;(client.request as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(403, 'Click again.', { error: 'needs_operation', detail: 'Click again.' }),
    )
    await expect(calls.handle(WRITE)).rejects.toThrow('Click again.')
    state.active = false
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
  })

  it('closing aborts what is in flight', async () => {
    const { calls, client } = setup({ active: false })
    let signal: AbortSignal | undefined
    ;(client.request as ReturnType<typeof vi.fn>).mockImplementationOnce((_p: string, _b: unknown, s: AbortSignal) => {
      signal = s
      return new Promise(() => undefined)
    })
    void calls.handle(READ).catch(() => undefined)
    await Promise.resolve()
    calls.closeAll()
    expect(signal?.aborted).toBe(true)
  })

  it('refuses a malformed request with no request made', async () => {
    const { calls, client } = setup({ active: true })
    await expect(calls.handle('x')).rejects.toThrow()
    await expect(calls.handle({ provider: 'salesforce', method: 'TRACE', path: 'query' })).rejects.toThrow()
    expect(client.request).not.toHaveBeenCalled()
  })

  it('a draft cannot call, with no request', async () => {
    const { calls, client } = setup({ active: true, ctx: { draft: true, surface: { kind: 'app', workspace: 'w', key: 'k' } } })
    await expect(calls.handle(READ)).rejects.toThrow("A draft can't use connections. Publish it first.")
    expect(client.request).not.toHaveBeenCalled()
  })

  it('an app that declares a report POST as a read runs it with no click', async () => {
    const app = { kind: 'app' as const, workspace: 'dimagi', key: 'crm' }
    const { calls, client } = setup({ active: false, ctx: { surface: app, declared: DECLARED } })
    await calls.handle({ provider: 'salesforce', method: 'POST', path: 'analytics/reports/00O1' })
    expect(client.request).toHaveBeenCalled()
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
  })
})

describe('the lease slides', () => {
  it('each write gives it another 120 seconds', async () => {
    const { calls, client, state } = setup({ active: true })
    await calls.handle(WRITE)
    state.active = false
    for (let i = 0; i < 4; i++) {
      state.now += 100_000
      await calls.handle(WRITE)
    }
    expect(client.openOperation).toHaveBeenCalledTimes(1)
    expect(client.request).toHaveBeenCalledTimes(5)
  })

  it('stops at 10 minutes from the click, however it is written to', async () => {
    const { calls, client, state } = setup({ active: true })
    await calls.handle(WRITE)
    state.active = false
    for (let i = 0; i < 5; i++) {
      state.now += 110_000
      await calls.handle(WRITE)
    }
    state.now += 60_000 // 610 seconds in, 60 after the last write
    await expect(calls.handle(WRITE)).rejects.toThrow('This needs a click.')
    expect(client.request).toHaveBeenCalledTimes(6)
  })

  it('does not trust the browser clock against the server clock', async () => {
    const { calls, client, state } = setup({ active: true })
    ;(client.openOperation as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      operation_id: 'skewed', expires_at: new Date(state.now - 3_600_000).toISOString(), hard_limit_at: new Date(state.now - 3_000_000).toISOString(), writes: [],
    })
    await expect(calls.handle(WRITE)).resolves.toBeTruthy()
    state.active = false
    await expect(calls.handle(WRITE)).resolves.toBeTruthy()
    expect(client.openOperation).toHaveBeenCalledTimes(1)
  })
})

describe('canopy.connection.begin', () => {
  it('opens the lease in the click and says what it covers, with no id for the frame', async () => {
    const { calls, client } = setup({ active: true })
    const out = await calls.begin()
    expect(Object.keys(out).sort()).toEqual(['expires_at', 'hard_limit_at', 'writes'])
    expect(out.writes).toEqual([{ provider: 'salesforce', method: 'PATCH', path: 'sobjects/Lead/*' }])
    expect(client.openOperation).toHaveBeenCalledWith(VIEW, expect.anything())
  })

  it('no click, no lease: the same page check as a write', async () => {
    const { calls, client } = setup({ active: false })
    await expect(calls.begin()).rejects.toThrow(NEEDS_CLICK)
    expect(client.openOperation).not.toHaveBeenCalled()
  })

  it('reads and dedups first, then writes on the lease with no second click', async () => {
    const { calls, client, state } = setup({ active: true })
    await calls.begin()
    state.active = false
    await calls.handle(READ)
    await calls.handle(WRITE)
    expect(client.openOperation).toHaveBeenCalledTimes(1)
    const write = (client.request as ReturnType<typeof vi.fn>).mock.calls[1][1]
    expect(write.operation_id).toBe('op1')
  })

  it('a new click opens a new window', async () => {
    const { calls, client } = setup({ active: true })
    await calls.begin()
    await calls.begin()
    expect(client.openOperation).toHaveBeenCalledTimes(2)
  })

  it('two asks in one click share one lease', async () => {
    const { calls, client } = setup({ active: true })
    await Promise.all([calls.begin(), calls.handle(WRITE)])
    expect(client.openOperation).toHaveBeenCalledTimes(1)
  })

  it('is refused where calls are: a draft, a public page, an unsaved view', async () => {
    await expect(setup({ active: true, ctx: { draft: true } }).calls.begin()).rejects.toThrow("A draft can't use connections.")
    await expect(setup({ active: true, ctx: { canConnect: false } }).calls.begin()).rejects.toThrow("A public page can't use connections.")
    await expect(setup({ active: true, ctx: { surface: null } }).calls.begin()).rejects.toThrow('Save this view to use connections.')
  })

  it('a refusal reaches the frame as a sentence', async () => {
    const { calls, client } = setup({ active: true })
    ;(client.openOperation as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(403, 'No changes declared.', { error: 'not_declared', detail: 'No changes declared.' }))
    await expect(calls.begin()).rejects.toThrow('No changes declared.')
  })
})

describe('the page queue', () => {
  /** A client that holds each call until told, and counts how many it holds at once. */
  function held() {
    const out = { now: 0, most: 0, releases: [] as Array<() => void> }
    const request = vi.fn(() => new Promise<{ status: number }>((resolve) => {
      out.now += 1
      out.most = Math.max(out.most, out.now)
      out.releases.push(() => { out.now -= 1; resolve({ status: 200 }) })
    }))
    return { ...out, track: out, request }
  }
  const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }

  it('30 concurrent reads all finish, at most 8 at once', async () => {
    expect(MAX_IN_FLIGHT).toBe(8)
    const { calls, client } = setup({ active: false })
    const h = held()
    client.request = h.request as unknown as ConnectionClient['request']
    const all = Promise.all(Array.from({ length: 30 }, () => calls.handle(READ)))
    await flush()
    expect(h.request).toHaveBeenCalledTimes(8)
    while (h.track.releases.length) {
      h.track.releases.shift()!()
      await flush()
    }
    await expect(all).resolves.toHaveLength(30)
    expect(h.request).toHaveBeenCalledTimes(30)
    expect(h.track.most).toBe(8)
  })

  it('goes first come, first served', async () => {
    const { calls, client } = setup({ active: false })
    const h = held()
    client.request = h.request as unknown as ConnectionClient['request']
    const all = Promise.all(Array.from({ length: 12 }, (_, i) => calls.handle({ ...READ, query: { q: String(i) } })))
    await flush()
    while (h.track.releases.length) {
      h.track.releases.shift()!()
      await flush()
    }
    await all
    expect(h.request.mock.calls.map((c: unknown[]) => (c[1] as { query: { q: string } }).query.q)).toEqual(Array.from({ length: 12 }, (_, i) => String(i)))
  })

  it("holds across a page's frames", async () => {
    const queue = new CallQueue()
    const h = held()
    const client = { request: h.request, openOperation: vi.fn() } as unknown as ConnectionClient
    const ctx = { surface: VIEW, declared: DECLARED, canConnect: true }
    const one = new ConnectionCalls(client, () => ctx, { queue })
    const two = new ConnectionCalls(client, () => ctx, { queue })
    void one.handle(READ).catch(() => undefined)
    for (let i = 0; i < 12; i++) void two.handle(READ).catch(() => undefined)
    await flush()
    expect(h.request).toHaveBeenCalledTimes(8)
    one.closeAll()
    two.closeAll()
  })

  it('frames share one queue unless told otherwise', () => {
    const client = { request: vi.fn(), openOperation: vi.fn() } as unknown as ConnectionClient
    const ctx = { surface: VIEW, declared: DECLARED, canConnect: true }
    expect(new ConnectionCalls(client, () => ctx).queue).toBe(new ConnectionCalls(client, () => ctx).queue)
  })

  it('a call that waits a minute rejects with a sentence, and was never sent', async () => {
    vi.useFakeTimers()
    try {
      expect(QUEUE_WAIT_MS).toBe(60_000)
      const { calls, client } = setup({ active: false })
      const h = held()
      client.request = h.request as unknown as ConnectionClient['request']
      for (let i = 0; i < 8; i++) void calls.handle(READ).catch(() => undefined)
      const late = calls.handle({ ...READ, query: { q: 'late' } })
      const caught = expect(late).rejects.toThrow(/waiting/)
      await vi.advanceTimersByTimeAsync(60_001)
      await caught
      expect(h.request).toHaveBeenCalledTimes(8)
      h.track.releases.shift()!()
      await vi.advanceTimersByTimeAsync(0)
      expect(h.request).toHaveBeenCalledTimes(8)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a write waits its turn too, and its lease is checked when it is sent', async () => {
    const { calls, client, state } = setup({ active: true })
    const h = held()
    client.request = h.request as unknown as ConnectionClient['request']
    for (let i = 0; i < 8; i++) void calls.handle(READ).catch(() => undefined)
    const fresh = calls.handle(WRITE)
    await flush()
    expect(h.request).toHaveBeenCalledTimes(8)
    state.now += 121_000 // the window closes while it waits
    h.track.releases.shift()!()
    await expect(fresh).rejects.toThrow(NEEDS_CLICK)
    expect(h.request).toHaveBeenCalledTimes(8)
  })

  it('closing drops what is waiting and frees what it held', async () => {
    const queue = new CallQueue()
    const h = held()
    const client = { request: h.request, openOperation: vi.fn() } as unknown as ConnectionClient
    const ctx = { surface: VIEW, declared: DECLARED, canConnect: true }
    const gone = new ConnectionCalls(client, () => ctx, { queue })
    const stays = new ConnectionCalls(client, () => ctx, { queue })
    for (let i = 0; i < 8; i++) void gone.handle(READ).catch(() => undefined)
    const dropped = gone.handle(READ)
    void stays.handle(READ).catch(() => undefined)
    await flush()
    expect(h.request).toHaveBeenCalledTimes(8)
    gone.closeAll()
    await expect(dropped).rejects.toThrow()
    await flush()
    expect(h.request).toHaveBeenCalledTimes(9)
    stays.closeAll()
  })
})

describe('a refusal that can be put right', () => {
  it('carries the server code and the connect link', async () => {
    const { calls, client } = setup({ active: false })
    const sentence = "Your Salesforce isn't connected in this workspace. Connect Salesforce in Settings → Connections."
    ;(client.request as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(409, sentence, { error: 'not_connected', detail: sentence, connect_url: 'https://canopy.test/auth/connections/salesforce/w/start/' }),
    )
    const e = await calls.handle(READ).catch((x: unknown) => x as Error & { error?: string; connect_url?: string })
    expect((e as Error).message).toBe(sentence)
    expect(e).toMatchObject({ error: 'not_connected', connect_url: 'https://canopy.test/auth/connections/salesforce/w/start/' })
  })

  it('carries no link when the server sent none', async () => {
    const { calls, client } = setup({ active: false })
    ;(client.request as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(409, 'Off.', { error: 'server_use_off', detail: 'Off.' }))
    const e = await calls.handle(READ).catch((x: unknown) => x as Error & { connect_url?: string })
    expect(e).toMatchObject({ error: 'server_use_off' })
    expect((e as { connect_url?: string }).connect_url).toBeUndefined()
  })
})

describe('pageClickedFrame', () => {
  const frame = { id: 'mine' } as unknown as Element
  const env = (o: { active?: boolean; focus?: boolean; el?: unknown }) => ({
    nav: { userActivation: o.active === undefined ? undefined : { isActive: o.active } },
    doc: { hasFocus: () => o.focus ?? true, activeElement: o.el ?? null },
  })
  it('activation with focus elsewhere is not a click on this screen', () => {
    expect(pageClickedFrame(frame, env({ active: true, el: { id: 'body' } }))).toBe(false)
  })
  it('a sibling frame holding focus fails', () => {
    expect(pageClickedFrame(frame, env({ active: true, el: { id: 'sibling' } }))).toBe(false)
  })
  it('the page without focus fails', () => {
    expect(pageClickedFrame(frame, env({ active: true, focus: false, el: frame }))).toBe(false)
  })
  it('focus on this frame with activation passes', () => {
    expect(pageClickedFrame(frame, env({ active: true, el: frame }))).toBe(true)
  })
  it('no activation, or no API, fails closed', () => {
    expect(pageClickedFrame(frame, env({ active: false, el: frame }))).toBe(false)
    expect(pageClickedFrame(frame, env({ el: frame }))).toBe(false)
    expect(pageClickedFrame(null, env({ active: true, el: null }))).toBe(false)
  })
})
