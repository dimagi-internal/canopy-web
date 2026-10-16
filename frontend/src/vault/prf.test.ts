// @vitest-environment jsdom
// The PRF assertion (canopy-web#1550): it asks the passkey for
// `prf.eval.first = prf_input`, returns the PRF output to the caller, and sends
// the server the assertion WITHOUT that output.
import { afterEach, describe, expect, it, vi } from 'vitest'

const { POST } = vi.hoisted(() => ({ POST: vi.fn() }))
vi.mock('@/api/client.v2', () => ({ apiV2: { POST } }))

const { assertWithPrf, browserSupportsPrf } = await import('./prf')
const { b64u } = await import('./zk')

function fakeCredential(prf: Uint8Array | null) {
  const buf = (s: string) => new TextEncoder().encode(s).buffer
  return {
    id: 'Y3JlZC0x',
    rawId: buf('cred-1'),
    type: 'public-key',
    response: { clientDataJSON: buf('{}'), authenticatorData: buf('a'), signature: buf('s'), userHandle: null },
    getClientExtensionResults: () => (prf ? { prf: { results: { first: prf.buffer } } } : {}),
  }
}

afterEach(() => vi.clearAllMocks())

describe('assertWithPrf', () => {
  it('evaluates prf_input, keeps the output, and strips it from what the server gets', async () => {
    const prfOut = new Uint8Array(32).fill(7)
    POST.mockImplementation(async (path: string) =>
      path.endsWith('/begin')
        ? { data: { options: { challenge: 'AAAA', allowCredentials: [{ id: 'Y3JlZC0x', type: 'public-key' }] } } }
        : { data: { active: true } })
    const get = vi.fn().mockResolvedValue(fakeCredential(prfOut))
    Object.defineProperty(navigator, 'credentials', { value: { get }, configurable: true })
    const input = b64u(new Uint8Array(32).fill(1))

    const out = await assertWithPrf(input)

    expect(out.cred).toBe('Y3JlZC0x')
    expect(Array.from(out.prf!)).toEqual(Array.from(prfOut))
    const ext = get.mock.calls[0][0].publicKey.extensions.prf.eval.first as Uint8Array
    expect(b64u(new Uint8Array(ext))).toBe(input)
    const sent = POST.mock.calls.find((c) => String(c[0]).endsWith('/finish'))![1].body.credential
    expect(sent.clientExtensionResults).toEqual({})
    expect(JSON.stringify(sent)).not.toContain('"prf"')
  })

  it('reports no PRF output as null (the caller says PRF is unsupported)', async () => {
    POST.mockImplementation(async (path: string) =>
      path.endsWith('/begin') ? { data: { options: { challenge: 'AAAA' } } } : { data: { active: true } })
    Object.defineProperty(navigator, 'credentials', {
      value: { get: vi.fn().mockResolvedValue(fakeCredential(null)) }, configurable: true })
    expect((await assertWithPrf(b64u(new Uint8Array(32)))).prf).toBeNull()
  })

  it('reads the browser capability hint where there is one', async () => {
    ;(globalThis as Record<string, unknown>).PublicKeyCredential = {
      getClientCapabilities: async () => ({ 'extension:prf': false }) }
    expect(await browserSupportsPrf()).toBe(false)
    ;(globalThis as Record<string, unknown>).PublicKeyCredential = {}
    expect(await browserSupportsPrf()).toBeNull()
  })
})
