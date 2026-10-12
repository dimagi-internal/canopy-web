// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { isWorkspaceRequired, mergeLists, pinnedUrl, withWorkspaceRetry } from './workspaceRetry'

const ORIGIN = 'https://canopy.example'

function problem(type: string, status = 422, workspaces: string[] = ['dimagi', 'marshellis']): Response {
  return new Response(JSON.stringify({ type, title: 't', status, extras: { workspaces } }), {
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

describe('mergeLists', () => {
  it('concatenates arrays, de-duplicating by id', () => {
    expect(mergeLists([[{ id: 1 }, { id: 2 }], [{ id: 2 }, { id: 3 }]])).toEqual([{ id: 1 }, { id: 2 }, { id: 3 }])
  })

  it('merges Ninja pages: items concatenated, counts added', () => {
    expect(mergeLists([{ items: [{ id: 'a' }], count: 1 }, { items: [{ id: 'b' }], count: 4 }])).toEqual({
      items: [{ id: 'a' }, { id: 'b' }],
      count: 5,
    })
  })

  it('refuses what is not a list', () => {
    expect(mergeLists([{ slug: 'x' }, { slug: 'y' }])).toBeNull()
    expect(mergeLists([[], { items: [] }])).toBeNull()
    expect(mergeLists([])).toBeNull()
  })
})

function recordingFetch(answer: (path: string, n: number) => Response) {
  const calls: string[] = []
  const base = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input)
    const path = new URL(url, ORIGIN).pathname
    calls.push(path)
    return answer(path, calls.length)
  }) as unknown as typeof fetch
  return { base, calls }
}

const REFUSED = 'https://x/problems/workspace-required'

describe('withWorkspaceRetry', () => {
  afterEach(() => window.history.replaceState(null, '', '/'))

  it('on a root page, loops a refused list over every named workspace and merges', async () => {
    window.history.replaceState(null, '', '/supervisor')
    const { base, calls } = recordingFetch((path, n) =>
      n === 1
        ? problem(REFUSED)
        : new Response(JSON.stringify({ items: [{ id: path }], count: 1 }), { status: 200 }),
    )
    const res = await withWorkspaceRetry(base)('/api/agents/')
    expect(res.status).toBe(200)
    expect(calls.sort()).toEqual(['/api/agents/', '/api/w/dimagi/agents/', '/api/w/marshellis/agents/'])
    const body = await res.json()
    expect(body.count).toBe(2)
    expect(body.items.map((r: { id: string }) => r.id).sort()).toEqual([
      '/api/w/dimagi/agents/',
      '/api/w/marshellis/agents/',
    ])
  })

  it('on a root page, loops over the covering pins when the refusal names them', async () => {
    window.history.replaceState(null, '', '/supervisor')
    const refusal = new Response(
      JSON.stringify({
        type: REFUSED, title: 't', status: 422,
        extras: { workspaces: ['connect', 'dimagi', 'marshellis'], pins: ['dimagi', 'marshellis'] },
      }),
      { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
    )
    const { base, calls } = recordingFetch((path, n) =>
      n === 1 ? refusal : new Response(JSON.stringify([{ id: path }]), { status: 200 }),
    )
    const res = await withWorkspaceRetry(base)('/api/agents/')
    expect(res.status).toBe(200)
    // connect is covered by the dimagi read; it is not read a second time.
    expect(calls.sort()).toEqual(['/api/agents/', '/api/w/dimagi/agents/', '/api/w/marshellis/agents/'])
  })

  it('on a root page, skips a workspace that refuses you the list, and surfaces any other failure', async () => {
    window.history.replaceState(null, '', '/activity')
    const gated = recordingFetch((path, n) =>
      n === 1 ? problem(REFUSED) : path.includes('/marshellis/') ? new Response('{}', { status: 403 }) : new Response('[{"id":1}]', { status: 200 }),
    )
    const ok = await withWorkspaceRetry(gated.base)('/api/harness/turns/')
    expect(await ok.json()).toEqual([{ id: 1 }])

    const broken = recordingFetch((path, n) =>
      n === 1 ? problem(REFUSED) : path.includes('/marshellis/') ? new Response('{}', { status: 500 }) : new Response('[]', { status: 200 }),
    )
    expect((await withWorkspaceRetry(broken.base)('/api/harness/turns/')).status).toBe(500)
  })

  it('on a tenant page, retries once pinned to that workspace', async () => {
    window.history.replaceState(null, '', '/w/marshellis/agents')
    const { base, calls } = recordingFetch((_p, n) => (n === 1 ? problem(REFUSED) : new Response('[]', { status: 200 })))
    const res = await withWorkspaceRetry(base)('/api/harness/turns/')
    expect(res.status).toBe(200)
    expect(calls).toEqual(['/api/harness/turns/', '/api/w/marshellis/harness/turns/'])
  })

  it('leaves writes and other 422s alone', async () => {
    const base = vi.fn(async () => problem(REFUSED)) as unknown as typeof fetch
    const res = await withWorkspaceRetry(base)('/api/shareouts/clear/', { method: 'POST', body: '{}' })
    expect(res.status).toBe(422)
    expect(base).toHaveBeenCalledTimes(1)

    const other = vi.fn(async () => problem('https://x/problems/validation')) as unknown as typeof fetch
    expect((await withWorkspaceRetry(other)('/api/agents/')).status).toBe(422)
    expect(other).toHaveBeenCalledTimes(1)
  })
})
