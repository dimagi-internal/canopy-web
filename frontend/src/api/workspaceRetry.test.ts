// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { isWorkspaceRequired, pinnedUrl, setDefaultWorkspace, withWorkspaceRetry } from './workspaceRetry'

const ORIGIN = 'https://canopy.example'

function problem(type: string, status = 422): Response {
  return new Response(JSON.stringify({ type, title: 't', status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  })
}

describe('pinnedUrl', () => {
  it('pins a flat API path onto /api/w/:ws/, keeping the query', () => {
    expect(pinnedUrl('/api/agents/?limit=5', 'dimagi', ORIGIN, '')).toBe(
      `${ORIGIN}/api/w/dimagi/agents/?limit=5`,
    )
  })

  it('keeps a deployment prefix', () => {
    expect(pinnedUrl('/canopy/api/harness/turns/', 'connect', ORIGIN, '/canopy')).toBe(
      `${ORIGIN}/canopy/api/w/connect/harness/turns/`,
    )
  })

  it('never re-pins a pinned path, or touches a non-API or foreign URL', () => {
    expect(pinnedUrl('/api/w/dimagi/agents/', 'connect', ORIGIN, '')).toBeNull()
    expect(pinnedUrl('/accounts/login/', 'dimagi', ORIGIN, '')).toBeNull()
    expect(pinnedUrl('https://elsewhere.example/api/agents/', 'dimagi', ORIGIN, '')).toBeNull()
  })
})

describe('isWorkspaceRequired', () => {
  it('matches only the workspace-required problem', async () => {
    expect(await isWorkspaceRequired(problem('https://x/problems/workspace-required'))).toBe(true)
    expect(await isWorkspaceRequired(problem('https://x/problems/validation'))).toBe(false)
    expect(await isWorkspaceRequired(problem('https://x/problems/workspace-required', 404))).toBe(false)
  })
})

describe('withWorkspaceRetry', () => {
  afterEach(() => setDefaultWorkspace(null))

  it('retries a refused flat GET once, pinned to the default workspace', async () => {
    setDefaultWorkspace('marshellis')
    const calls: string[] = []
    const base = vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input)
      calls.push(new URL(url, ORIGIN).pathname)
      return calls.length === 1
        ? problem('https://x/problems/workspace-required')
        : new Response('[]', { status: 200 })
    }) as unknown as typeof fetch
    const res = await withWorkspaceRetry(base)('/api/agents/')
    expect(res.status).toBe(200)
    expect(calls).toEqual(['/api/agents/', '/api/w/marshellis/agents/'])
  })

  it('leaves writes and other 422s alone', async () => {
    setDefaultWorkspace('dimagi')
    const base = vi.fn(async () => problem('https://x/problems/workspace-required')) as unknown as typeof fetch
    const res = await withWorkspaceRetry(base)('/api/shareouts/clear/', { method: 'POST', body: '{}' })
    expect(res.status).toBe(422)
    expect(base).toHaveBeenCalledTimes(1)

    const other = vi.fn(async () => problem('https://x/problems/validation')) as unknown as typeof fetch
    expect((await withWorkspaceRetry(other)('/api/agents/')).status).toBe(422)
    expect(other).toHaveBeenCalledTimes(1)
  })
})
