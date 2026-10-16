// The step-up retry (canopy-web#1538): a 403 `step_up_required` prompts once
// and replays the ORIGINAL request — body included — exactly once.
import { afterEach, describe, expect, it, vi } from 'vitest'
import createClient from 'openapi-fetch'
import { requestStepUp, setStepUpHandler, stepUpFromBody, stepUpMiddleware, withStepUp } from './stepUp'
import { base64urlToBuffer, bufferToBase64url, toRequestOptions } from './webauthn'

const refusal = () =>
  new Response(JSON.stringify({ error: 'step_up_required', has_passkey: true, status: 403, detail: 'Confirm it’s you' }), {
    status: 403,
    headers: { 'Content-Type': 'application/problem+json' },
  })
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

afterEach(() => {
  setStepUpHandler(null)
  vi.restoreAllMocks()
})

describe('stepUpFromBody', () => {
  it('recognises only a 403 carrying the step_up_required member', () => {
    expect(stepUpFromBody(403, { error: 'step_up_required', has_passkey: false })).toEqual({ hasPasskey: false, detail: undefined })
    expect(stepUpFromBody(403, { error: 'browser_session_required' })).toBeNull()
    expect(stepUpFromBody(401, { error: 'step_up_required' })).toBeNull()
    expect(stepUpFromBody(403, 'nope')).toBeNull()
  })
})

describe('stepUpMiddleware', () => {
  function client(fetchImpl: (r: Request) => Promise<Response>) {
    vi.stubGlobal('fetch', vi.fn(fetchImpl))
    const c = createClient<Record<string, never>>({ baseUrl: 'http://x', fetch: (r: Request) => globalThis.fetch(r) })
    c.use(stepUpMiddleware())
    return c
  }

  it('prompts, then replays the original write with its body', async () => {
    const seen: string[] = []
    let calls = 0
    const c = client(async (r) => {
      seen.push(`${r.method} ${await r.text()}`)
      return ++calls === 1 ? refusal() : ok({ value: 'shh' })
    })
    const prompt = vi.fn().mockResolvedValue(undefined)
    setStepUpHandler(prompt)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const res = await (c as any).POST('/api/agents/eva/credentials/k/reveal', { body: { a: 1 } })
    expect(prompt).toHaveBeenCalledWith({ hasPasskey: true, detail: 'Confirm it’s you' })
    expect(res.data).toEqual({ value: 'shh' })
    expect(seen).toEqual(['POST {"a":1}', 'POST {"a":1}'])
  })

  it('hands back the original 403 when the person cancels', async () => {
    const c = client(async () => refusal())
    setStepUpHandler(() => Promise.reject(new Error('cancelled')))
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const res = await (c as any).PUT('/api/agents/eva/credentials', { body: { values: {} } })
    expect(res.response.status).toBe(403)
    expect(res.error.error).toBe('step_up_required')
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
  })

  it('retries only once — a second refusal is final', async () => {
    const c = client(async () => refusal())
    setStepUpHandler(() => Promise.resolve())
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const res = await (c as any).POST('/x', { body: {} })
    expect(res.response.status).toBe(403)
    expect(globalThis.fetch).toHaveBeenCalledTimes(2)
  })

  it('leaves a refusal alone when no prompt is mounted', async () => {
    const c = client(async () => refusal())
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const res = await (c as any).POST('/x', { body: {} })
    expect(res.response.status).toBe(403)
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
  })
})

describe('requestStepUp', () => {
  it('shares one prompt between concurrent callers', async () => {
    let release!: () => void
    const prompt = vi.fn(() => new Promise<void>((r) => (release = r)))
    setStepUpHandler(prompt)
    const a = requestStepUp({ hasPasskey: true })
    const b = requestStepUp({ hasPasskey: true })
    release()
    await Promise.all([a, b])
    expect(prompt).toHaveBeenCalledTimes(1)
  })
})

describe('withStepUp', () => {
  it('re-runs a raw fetch thunk after the prompt', async () => {
    setStepUpHandler(() => Promise.resolve())
    const thunk = vi.fn().mockResolvedValueOnce(refusal()).mockResolvedValueOnce(ok({ ok: true }))
    const res = await withStepUp(thunk)
    expect(res.status).toBe(200)
    expect(thunk).toHaveBeenCalledTimes(2)
  })
})

describe('webauthn helpers', () => {
  it('round-trips base64url without padding', () => {
    const bytes = new Uint8Array([0, 251, 255, 62, 63, 1])
    const s = bufferToBase64url(bytes)
    expect(s).not.toMatch(/[+/=]/)
    expect(Array.from(new Uint8Array(base64urlToBuffer(s)))).toEqual(Array.from(bytes))
  })

  it('decodes the challenge and allowCredentials ids for navigator.credentials.get', () => {
    const opts = toRequestOptions({
      challenge: bufferToBase64url(new Uint8Array([1, 2, 3])),
      rpId: 'canopy.dimagi.com',
      allowCredentials: [{ id: bufferToBase64url(new Uint8Array([9])), type: 'public-key', transports: ['internal'] }],
      userVerification: 'required',
    })
    expect(Array.from(new Uint8Array(opts.challenge as ArrayBuffer))).toEqual([1, 2, 3])
    expect(opts.rpId).toBe('canopy.dimagi.com')
    expect(Array.from(new Uint8Array(opts.allowCredentials![0].id as ArrayBuffer))).toEqual([9])
    expect(opts.allowCredentials![0].transports).toEqual(['internal'])
  })
})
