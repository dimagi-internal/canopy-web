import { describe, expect, it, vi } from 'vitest'

const GET = vi.fn(async () => ({ data: [], error: undefined, response: new Response() }))
const POST = vi.fn(async () => ({
  data: { task: {}, action: {}, turn_ids: [] },
  error: undefined,
  response: new Response(),
}))
vi.mock('./client.v2', () => ({ apiV2: { GET, POST }, WORKSPACE_HEADER: 'X-Workspace' }))
const { listTasks, actOnTask } = await import('./agents')

describe('task client', () => {
  it('passes filters as query params', async () => {
    await listTasks('eva', { waiting: 'me', project: 'P2' })
    expect(GET).toHaveBeenCalledWith('/api/agents/{slug}/tasks/', {
      params: { path: { slug: 'eva' }, query: { waiting: 'me', project: 'P2' } },
    })
  })
  it('acts by ext_id', async () => {
    await actOnTask('eva', 'T2', 'reply', 'Tuesday')
    expect(POST).toHaveBeenCalledWith('/api/agents/{slug}/tasks/{ref}/actions', {
      params: { path: { slug: 'eva', ref: 'T2' } },
      body: { action: 'reply', comment: 'Tuesday' },
    })
  })
})
