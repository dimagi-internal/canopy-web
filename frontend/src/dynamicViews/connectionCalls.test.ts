import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/problem'
import { ConnectionCalls, type CallContext, type ConnectionClient } from './connectionCalls'

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
    openOperation: vi.fn(async () => ({ operation_id: `op${(client.openOperation as ReturnType<typeof vi.fn>).mock.calls.length}`, expires_at: new Date(state.now + 120_000).toISOString() })),
  }
  const ctx: CallContext = { surface: VIEW, declared: DECLARED, canConnect: true, ...opts.ctx }
  const calls = new ConnectionCalls(client, () => ctx, { isActive: () => state.active, now: opts.now ?? (() => state.now) })
  return { calls, client, state, ctx }
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
})
