import { describe, expect, it, vi } from 'vitest'

const GET = vi.fn(async () => ({ data: [{ id: 'r1' }], error: undefined, response: new Response() }))
vi.mock('./client.v2', () => ({ apiV2: { GET }, WORKSPACE_HEADER: 'X-Workspace' }))
const { listRunners, recentRunners } = await import('./harness')

describe('listRunners', () => {
  it('shares one read between callers that ask while it is in flight', async () => {
    const [a, b] = await Promise.all([listRunners(), listRunners()])
    expect(GET).toHaveBeenCalledTimes(1)
    expect(a).toBe(b)
    expect(recentRunners(60_000)).toBe(a)
  })

  it('reads again once the last read has settled', async () => {
    GET.mockClear()
    await listRunners()
    await listRunners()
    expect(GET).toHaveBeenCalledTimes(2)
  })
})
